# WebRTC Video Track Architecture for MuseTalk Avatar Streaming

**Author**: Opus (Claude Code) + Chef (David)
**Date**: March 18, 2026
**Status**: DESIGN — Phase 2 of Sovereign FaceTime

## Overview

This document describes the architecture for streaming MuseTalk-generated avatar
video frames from the GPU server (192.168.0.100) to the browser over WebRTC,
combined with TTS audio in a single `RTCPeerConnection`. The design extends the
existing SKComm WebRTC transport (currently data-channel-only) to support media
tracks while preserving CapAuth-signed SDP offers, TURN relay via
turn.skworld.io, and the sovereign signaling broker.

## Architecture Diagram

```
Browser (WebRTC consumer)
    |
    |  RTCPeerConnection
    |    - VideoTrack (H.264 or VP8 from MuseTalk frames)
    |    - AudioTrack (Opus-encoded TTS audio)
    |    - DataChannel "skcomm" (control messages, captions, emotion metadata)
    |
    |  ICE: STUN → direct LAN / TURN relay (turn.skworld.io)
    |  SDP: CapAuth PGP-signed, via /webrtc/ws signaling broker
    |
    ▼
GPU Server (192.168.0.100) — aiortc RTCPeerConnection
    |
    ├─ MuseTalkVideoTrack (VideoStreamTrack subclass)
    |    - Receives RGBA/BGR frames from MuseTalk inference
    |    - Converts to av.VideoFrame (yuv420p)
    |    - Yields at 20 FPS with proper PTS/time_base
    |
    ├─ TTSAudioTrack (AudioStreamTrack subclass)
    |    - Receives PCM from Chatterbox TTS
    |    - Chunks into 20ms Opus frames (960 samples @ 48kHz)
    |    - Yields av.AudioFrame with monotonic timestamps
    |
    └─ DataChannel "skcomm" (existing)
         - Transcript text, emotion state, control signals
```

## 1. Creating a Video MediaStreamTrack from MuseTalk Frames

### MuseTalk Output Format

MuseTalk produces BGR numpy arrays (OpenCV format), typically 256x256 or
512x512, at 15-20 FPS depending on GPU load. Each inference call takes the
current audio segment and a reference portrait, producing a lip-synced frame.

### Frame Pipeline

```
MuseTalk inference (BGR numpy, 256x256)
    → cv2.resize to 720x720 or 1280x720
    → cv2.cvtColor BGR→RGB
    → av.VideoFrame.from_ndarray(rgb, format="rgb24")
    → frame.reformat(format="yuv420p")  # WebRTC-required colorspace
    → yield from VideoStreamTrack.recv()
```

### Key Design Decisions

1. **Resolution**: 720p (1280x720) for full-screen, 480p (854x480) for
   bandwidth-constrained. MuseTalk native resolution (256x256) is upscaled with
   bilinear interpolation. The avatar is composited onto a background template
   at the target resolution.

2. **Frame rate**: Target 20 FPS. MuseTalk can sustain this on RTX 5060 Ti.
   If inference falls behind, the track repeats the last frame (freeze rather
   than skip) to maintain smooth PTS progression.

3. **Colorspace**: yuv420p is mandatory for WebRTC. All frames must be
   reformatted before yielding.

4. **Thread safety**: MuseTalk inference runs in a dedicated thread/process.
   Frames are passed to the aiortc track via an `asyncio.Queue` with a max
   depth of 3 frames (60ms at 20 FPS) to bound memory and latency.

## 2. Codec Selection: H.264 vs VP8 vs VP9

| Codec | Browser Support | HW Encode (RTX 5060) | Latency | Bandwidth | Recommendation |
|-------|----------------|---------------------|---------|-----------|----------------|
| H.264 | Universal (all browsers + mobile) | NVENC available | Lowest | Best at low bitrate | **Primary** |
| VP8 | Chrome, Firefox, Edge | No HW encode | Low | Good | **Fallback** |
| VP9 | Chrome, Firefox, Edge | NVENC limited | Higher (more compression) | Best | Not recommended for real-time |

### Recommendation: H.264 Primary, VP8 Fallback

- **H.264 Baseline Profile**: Supported everywhere. aiortc uses x264 by default
  (software encode). NVENC hardware encoding is possible by building a custom
  encoder, but software x264 at 720p@20fps is well within CPU budget.
- **VP8**: aiortc default codec. Good fallback if H.264 negotiation fails.
- **VP9**: Too much encode latency for real-time avatar streaming. Skip.

### SDP Codec Preference

When creating the offer, prefer H.264 by reordering the codec list:

```python
from aiortc import RTCRtpSender

# Force H.264 preference in SDP
capabilities = RTCRtpSender.getCapabilities("video")
h264_codecs = [c for c in capabilities.codecs if "H264" in c.mimeType]
other_codecs = [c for c in capabilities.codecs if "H264" not in c.mimeType]
# Transceiver preference: H.264 first
transceiver.setCodecPreferences(h264_codecs + other_codecs)
```

## 3. aiortc VideoStreamTrack Implementation

aiortc provides `MediaStreamTrack` as the base class, with `VideoStreamTrack`
as the convenience subclass for video. The key method to override is `recv()`
which must return an `av.VideoFrame` with correct timing.

### Timing Model

aiortc expects `recv()` to be called in a loop. The track must:

1. Set `frame.pts` to a monotonically increasing value.
2. Set `frame.time_base` to `fractions.Fraction(1, VIDEO_CLOCK_RATE)` where
   `VIDEO_CLOCK_RATE = 90000` (standard RTP video clock).
3. Pace itself to the target FPS. If `recv()` returns too fast, frames
   pile up in the jitter buffer. If too slow, the browser sees freezes.

### Frame Pacing Strategy

The track maintains a wall-clock start time and computes the expected PTS for
each frame. If a new MuseTalk frame is not ready, the previous frame is
re-yielded with an updated PTS (frame repeat). If inference is faster than
real-time, frames are dropped.

```
PTS = (frame_count * VIDEO_CLOCK_RATE) // TARGET_FPS
expected_wall = start_time + (frame_count / TARGET_FPS)
actual_wall = time.monotonic()
if actual_wall < expected_wall:
    await asyncio.sleep(expected_wall - actual_wall)
```

### Idle Frame

When no speech is being synthesized (TTS silent), the avatar should still be
"alive" with subtle idle animation (blinking, slight head movement). Options:

- **Static portrait**: Lowest cost. Just yield the same frame.
- **Idle loop**: Pre-rendered 3-5 second loop of subtle movement (blink cycle).
  Cycles continuously. MuseTalk can generate this from silence audio.
- **Live idle**: Run MuseTalk with ambient noise input for organic micro-movements.

Recommendation: Start with static portrait, add idle loop in Phase 3.

## 4. Combining Audio + Video in a Single RTCPeerConnection

### Track Addition Order

```python
from aiortc import RTCPeerConnection

pc = RTCPeerConnection(configuration=rtc_config)

# Add video track (MuseTalk avatar)
video_track = MuseTalkVideoTrack(frame_queue, fps=20)
pc.addTrack(video_track)

# Add audio track (TTS output)
audio_track = TTSAudioTrack(audio_queue, sample_rate=48000)
pc.addTrack(audio_track)

# Create data channel for text/control (existing pattern)
channel = pc.createDataChannel("skcomm", ordered=True)
```

### Audio Track Details

aiortc audio uses Opus codec (mandatory in WebRTC). The audio track must
yield `av.AudioFrame` objects:

- **Sample rate**: 48000 Hz (Opus native)
- **Frame duration**: 20ms (960 samples) — standard Opus frame
- **Layout**: mono (avatar speech is mono)
- **Format**: s16 (16-bit signed PCM)

TTS engines (Chatterbox, Piper) output at various sample rates (22050, 24000,
44100). The audio track resamples to 48kHz before framing.

### Synchronization

Audio-video sync is handled by aiortc's RTP timestamp system. As long as both
tracks maintain accurate PTS relative to their respective clock rates (48000
for audio, 90000 for video), the browser's jitter buffer handles lip-sync.

For MuseTalk specifically: the audio chunk that drives lip-sync inference is
the *same* audio being sent on the audio track. This gives inherent sync —
the video frames are generated *from* the audio, so they are naturally aligned.

The critical path is:
1. TTS generates audio chunk (e.g., 500ms of speech).
2. Audio chunk is enqueued to both the audio track AND MuseTalk.
3. MuseTalk generates N video frames from that chunk.
4. Both tracks yield their frames with aligned timestamps.

### Pipeline Coordination

```python
async def on_tts_chunk(audio_pcm: bytes, start_pts: int):
    """Called when TTS produces a chunk of audio."""
    # Feed to audio track for WebRTC
    await audio_queue.put(audio_pcm)

    # Feed to MuseTalk for lip-sync frame generation
    frames = await musetalk.generate_frames(audio_pcm, portrait)
    for frame in frames:
        await video_frame_queue.put(frame)
```

## 5. Browser-Side: Receiving and Rendering WebRTC Video + Audio

### Signaling Flow (Extends Existing)

The browser connects to the signaling broker at `/webrtc/ws` using the same
protocol as the existing data-channel flow. The only difference is that the
SDP offer from the server now includes video and audio media descriptions
in addition to the data channel.

```javascript
// Connect to signaling broker (existing pattern)
const ws = new WebSocket("wss://skchat.skworld.io/webrtc/ws?room=skcomm-CCBE..&peer=BROWSER_FP");

// Create peer connection with TURN
const pc = new RTCPeerConnection({
    iceServers: [
        { urls: "stun:stun.l.google.com:19302" },
        { urls: "turn:turn.skworld.io:3478", username: hmacUser, credential: hmacCred }
    ]
});

// Handle incoming tracks
pc.ontrack = (event) => {
    const stream = event.streams[0];
    if (event.track.kind === "video") {
        document.getElementById("avatar-video").srcObject = stream;
    } else if (event.track.kind === "audio") {
        document.getElementById("avatar-audio").srcObject = stream;
    }
};

// Handle data channel (existing pattern)
pc.ondatachannel = (event) => {
    const dc = event.channel;
    dc.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === "transcript") updateCaptions(msg);
        if (msg.type === "emotion") updateEmotionUI(msg);
    };
};
```

### HTML Structure

```html
<div id="facetime-container">
    <!-- Avatar video (from MuseTalk) -->
    <video id="avatar-video" autoplay playsinline muted></video>

    <!-- Avatar audio (from TTS) — separate element for volume control -->
    <audio id="avatar-audio" autoplay></audio>

    <!-- Captions overlay (from data channel) -->
    <div id="captions-overlay"></div>

    <!-- User's camera (optional, Phase 3) -->
    <video id="user-camera" autoplay playsinline muted></video>
</div>
```

Note: The video element has `muted` because avatar audio comes from the
separate `<audio>` element. This avoids autoplay restrictions (muted video
autoplays without user gesture, audio requires a user interaction first).

### Autoplay Policy Handling

Browsers block autoplay of audio. The UI must have a "Start Call" button
that triggers a user gesture:

```javascript
document.getElementById("start-call").onclick = async () => {
    // User gesture unlocks audio playback
    const audioEl = document.getElementById("avatar-audio");
    await audioEl.play();  // Unlocks audio context

    // Now initiate WebRTC
    await startSignaling();
};
```

## 6. Latency Considerations

### End-to-End Latency Budget

| Stage | Time | Notes |
|-------|------|-------|
| User speech → STT | 200-500ms | SenseVoice on GPU |
| STT → LLM response start | 200-500ms | Anthropic streaming |
| LLM → TTS first audio chunk | 200-400ms | Chatterbox streaming |
| TTS → MuseTalk first frame | 50-100ms | Single frame inference |
| WebRTC encode + transmit | 50-150ms | LAN: 50ms, WAN+TURN: 150ms |
| Browser jitter buffer | 50-100ms | Adaptive, typically 2-3 frames |
| **Total (first frame visible)** | **750-1750ms** | |

### Optimization Strategies

1. **Streaming TTS**: Don't wait for full utterance. Generate audio in chunks
   (sentence by sentence) and start MuseTalk + WebRTC delivery for the first
   chunk while later chunks are still being synthesized.

2. **Jitter buffer tuning**: aiortc's default jitter buffer is conservative.
   For LAN use, reduce `RTCConfiguration.iceTransportPolicy` and consider
   setting `playoutDelayHint` on the browser side.

3. **Frame dropping**: If MuseTalk falls behind, drop frames rather than
   queuing them. A 1-frame queue (latest frame wins) prevents accumulating
   latency.

4. **Idle preload**: When the user starts speaking (VAD triggers), pre-warm
   MuseTalk by generating idle frames. This ensures the GPU pipeline is hot
   when real lip-sync frames are needed.

5. **Keyframe interval**: Set keyframe interval to 2 seconds (40 frames at
   20 FPS). Frequent keyframes help with recovery after packet loss but
   increase bandwidth. Tune based on network conditions.

### Buffering Strategy

```
Server side:
    MuseTalk → asyncio.Queue(maxsize=2)  → VideoStreamTrack.recv()
    TTS PCM  → asyncio.Queue(maxsize=10) → AudioStreamTrack.recv()

    Video queue is intentionally small (2 frames = 100ms at 20 FPS).
    Older frames are dropped if queue is full (latest-wins).

    Audio queue is larger (10 frames = 200ms at 20ms/frame) because
    audio discontinuity is more perceptible than video frame drops.

Browser side:
    RTCPeerConnection → MediaStream → <video> / <audio> elements
    Browser handles its own jitter buffer (typically 50-150ms).
    No additional application-level buffering needed.
```

## 7. Bandwidth Requirements

### Video Bandwidth (720p @ 20 FPS, H.264)

Avatar video is *extremely* compressible because:
- The background is static (only the face region changes).
- Frame-to-frame delta is small (lip movements, not full motion).
- The source resolution is upscaled from 256x256 (low spatial detail).

| Quality | Bitrate | Monthly (1hr/day) | Notes |
|---------|---------|-------------------|-------|
| Low (480p, q28) | 200-400 kbps | ~5 GB | Adequate for avatar |
| Medium (720p, q24) | 500-800 kbps | ~12 GB | **Recommended** |
| High (720p, q20) | 1-1.5 Mbps | ~22 GB | Diminishing returns for avatar |

### Audio Bandwidth (Opus)

| Quality | Bitrate | Notes |
|---------|---------|-------|
| Speech | 24-32 kbps | Default Opus for speech, excellent quality |
| Wideband | 48-64 kbps | Overkill for TTS avatar speech |

### Total Bandwidth

**Recommended**: 500-800 kbps video + 32 kbps audio = **~600-850 kbps**

This is well within LAN capacity and comfortable over WAN. For comparison,
a standard Zoom call uses 1.5-3 Mbps for video.

### Adaptive Bitrate

aiortc supports bandwidth estimation. The video encoder can be configured
to adapt:

```python
# In the video transceiver
sender = pc.getSenders()[0]  # video sender
params = sender.getParameters()
params.encodings[0].maxBitrate = 800_000  # 800 kbps
await sender.setParameters(params)
```

## 8. Fallback: WebSocket Binary Frames

If WebRTC is unavailable (strict corporate firewall blocking STUN/TURN,
aiortc not installed, etc.), fall back to WebSocket binary streaming.

### Fallback Protocol

```
Browser → WS /ws/facetime/{agent}

Server sends:
    Binary frames: MJPEG-encoded video frames (JPEG per frame)
    Text frames:   JSON control messages (same as data channel protocol)

Frame format (binary):
    [4 bytes: frame_type (0x01=video, 0x02=audio)]
    [4 bytes: timestamp_ms (uint32 LE)]
    [4 bytes: payload_length (uint32 LE)]
    [N bytes: payload]

    Video payload: JPEG-encoded frame (quality 75, ~15-30 KB per frame at 720p)
    Audio payload: Opus-encoded packet (from opuslib)
```

### Bandwidth Impact

MJPEG over WebSocket is ~3-5x less efficient than H.264 over WebRTC:
- Each frame is independently compressed (no inter-frame prediction).
- No RTP-level congestion control.
- WebSocket framing overhead.

At 720p@20fps: MJPEG = ~3-5 Mbps vs H.264 = ~600 kbps.

At 480p@15fps: MJPEG = ~1.5-2.5 Mbps — acceptable for LAN fallback.

### When to Fall Back

```javascript
async function connectFaceTime(agentName) {
    try {
        // Try WebRTC first
        await connectWebRTC(agentName);
    } catch (e) {
        console.warn("WebRTC failed, falling back to WebSocket:", e);
        // ICE failed, or aiortc not available on server
        connectWebSocketFallback(agentName);
    }
}
```

## 9. Integration Points with Existing SKComm / skchat

### SKComm WebRTC Transport Extension

The existing `WebRTCTransport` in `skcomm/transports/webrtc.py` only creates
data channels. The video/audio tracks are a separate concern — they belong
in a new `FaceTimeSession` class that uses the same signaling infrastructure
but creates its own `RTCPeerConnection` with media tracks.

**Do NOT modify the existing WebRTCTransport.** It serves a different purpose
(reliable ordered messaging). Instead, create a parallel media session class
that reuses:

- The signaling broker (`/webrtc/ws` room protocol)
- ICE server configuration (`_build_ice_servers()`)
- TURN credential derivation (`_derive_turn_credentials()`)
- CapAuth SDP signing

### New Files (Proposed)

| File | Purpose |
|------|---------|
| `skcomm/transports/webrtc_media.py` | `FaceTimeSession` — media track management |
| `skcomm/transports/video_track.py` | `MuseTalkVideoTrack` — aiortc VideoStreamTrack |
| `skcomm/transports/audio_track.py` | `TTSAudioTrack` — aiortc AudioStreamTrack |
| `skchat/facetime.py` | `FaceTimeManager` — session lifecycle, MuseTalk coordination |
| `skchat/static/facetime.html` | Browser UI |
| `skchat/static/facetime.js` | WebRTC client logic |

### SKVoice Integration

SKVoice (192.168.0.100:18800) currently handles the full voice pipeline. For
FaceTime, SKVoice gains a new endpoint:

```
WS /ws/facetime/{agent_name}
```

This endpoint:
1. Runs the voice pipeline (STT → LLM → TTS) as today.
2. Feeds TTS audio to both the WebRTC audio track AND MuseTalk.
3. MuseTalk frames go to the WebRTC video track.
4. The WebRTC peer connection is managed locally on the GPU box.

skchat remains a thin proxy:
```
Browser ↔ skchat (WS proxy) ↔ SKVoice GPU (WebRTC origination)
```

Wait — this is wrong. WebRTC should originate from wherever has the media.
Two architectures are possible:

**Option A: WebRTC from GPU box (recommended)**
- Browser connects directly to GPU box for WebRTC (STUN/ICE handles NAT).
- Signaling goes through skchat/skcomm broker (lightweight).
- GPU box has the video frames and audio — no extra hop.
- TURN relay handles cases where direct connection fails.

**Option B: WebRTC from skchat gateway, media proxied from GPU**
- skchat originates the WebRTC connection.
- Video/audio frames are streamed from GPU to skchat via internal WS/gRPC.
- skchat encodes and sends via WebRTC.
- Adds latency and CPU load on the gateway box.

**Decision: Option A.** The GPU box runs the aiortc peer connection.
Signaling (SDP/ICE exchange) routes through the existing broker. Media
flows directly between GPU box and browser via ICE.

### Signaling Protocol Extension

Add a new signal type to the `/webrtc/ws` protocol:

```json
{
    "type": "signal",
    "to": "<browser_fingerprint>",
    "data": {
        "sdp": "...",
        "type": "offer",
        "media_type": "facetime"  // NEW: distinguishes from data-only offers
    }
}
```

The browser uses `media_type` to know this SDP includes video/audio tracks
and should be rendered in the FaceTime UI rather than handled as a data
channel connection.

## 10. Security Considerations

All existing security properties are preserved:

- **CapAuth SDP signing**: SDP offers/answers carry PGP signatures over the
  SDP text. The DTLS fingerprint in the SDP is bound to the signature.
  Video/audio tracks use the same DTLS-SRTP encryption as data channels.

- **TURN authentication**: HMAC-SHA1 time-limited credentials via
  `_derive_turn_credentials()`. No change needed.

- **No media through signaling relay**: The signaling broker only sees
  encrypted SDP/ICE messages. All media (video, audio) flows directly
  between peers via DTLS-SRTP.

- **Browser fingerprint**: In Phase 3 (bidirectional video), the browser's
  camera stream is encrypted end-to-end via DTLS-SRTP. The server never
  has access to unencrypted camera frames unless explicitly forwarded.

## Appendix: MuseTalk Integration Notes

### MuseTalk API (Expected)

MuseTalk is typically run as a Python module. For integration, wrap it in a
simple async interface:

```python
class MuseTalkEngine:
    """Wraps MuseTalk inference for real-time lip-sync generation."""

    async def load(self, portrait_path: str) -> None:
        """Load a reference portrait for the current agent."""

    async def generate_frames(
        self, audio_pcm: bytes, sample_rate: int = 16000
    ) -> list[np.ndarray]:
        """Generate lip-synced video frames from audio.

        Args:
            audio_pcm: Raw PCM audio (16-bit, mono).
            sample_rate: Audio sample rate.

        Returns:
            List of BGR numpy arrays (one per frame at target FPS).
        """

    async def get_idle_frame(self) -> np.ndarray:
        """Return a single idle (mouth closed) portrait frame."""
```

### Portrait Management

Each agent has a portrait stored at:
```
~/.skcapstone/agents/{agent}/avatar/portrait.png
```

The portrait is loaded once when a FaceTime session starts. MuseTalk uses
it as the reference face for all lip-sync generation in that session.

### VRAM Sharing

MuseTalk requires ~4-6 GB VRAM. When combined with Chatterbox TTS (~2-3 GB)
and SenseVoice STT (~2-3 GB), total is ~10 GB — within the 16 GB budget
of the RTX 5060 Ti.

ComfyUI must be unloaded during FaceTime sessions (or use VRAM offloading).
A simple mutex/semaphore in SKVoice can prevent VRAM contention:

```python
gpu_semaphore = asyncio.Semaphore(1)

async def start_facetime():
    await gpu_semaphore.acquire()
    # Load MuseTalk, reserve VRAM
    ...

async def stop_facetime():
    # Unload MuseTalk, free VRAM
    gpu_semaphore.release()
```
