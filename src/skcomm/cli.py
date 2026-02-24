"""
SKComm CLI — sovereign agent messaging from the command line.

Send, receive, and manage messages across all transports
from any terminal. Works standalone or alongside the daemon.

Usage:
    skcomm send lumina "Hello from the terminal"
    skcomm receive
    skcomm status
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import click

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()
except ImportError:
    console = None  # type: ignore[assignment]

from . import __version__
from .config import SKCOMM_HOME

_HOME = SKCOMM_HOME


def _print(msg: str) -> None:
    """Print using rich if available, else plain click.echo."""
    if console:
        console.print(msg)
    else:
        click.echo(msg)


@click.group()
@click.version_option(version=__version__, prog_name="skcomm")
def main():
    """SKComm — Sovereign Agent Communication.

    Transport-agnostic encrypted messaging.
    One message. Many paths. Always delivered.
    """


@main.command()
@click.argument("recipient")
@click.argument("message")
@click.option("--config", "-c", default=None, help="Config file path.")
@click.option(
    "--mode",
    "-m",
    type=click.Choice(["failover", "broadcast", "stealth", "speed"]),
    default=None,
    help="Override routing mode.",
)
@click.option("--thread", "-t", default=None, help="Thread ID for conversation grouping.")
@click.option("--reply-to", default=None, help="Envelope ID this replies to.")
@click.option(
    "--urgency",
    "-u",
    type=click.Choice(["low", "normal", "high", "critical"]),
    default="normal",
)
def send(
    recipient: str,
    message: str,
    config: Optional[str],
    mode: Optional[str],
    thread: Optional[str],
    reply_to: Optional[str],
    urgency: str,
):
    """Send a message to another agent.

    Messages are routed through all configured transports
    based on the routing mode (default: failover).

    Examples:

        skcomm send lumina "Sync complete on desktop"

        skcomm send opus "Need review" --urgency high
    """
    from .core import SKComm
    from .models import RoutingMode, Urgency

    comm = SKComm.from_config(config)
    mode_enum = RoutingMode(mode) if mode else None
    urgency_enum = Urgency(urgency)

    report = comm.send(
        recipient=recipient,
        message=message,
        mode=mode_enum,
        thread_id=thread,
        in_reply_to=reply_to,
        urgency=urgency_enum,
    )

    if report.delivered:
        via = report.successful_transport or "unknown"
        _print(f"\n  [green]Sent[/] to [bold]{recipient}[/] via {via}")
        for a in report.attempts:
            if a.success:
                _print(f"    [dim]{a.transport_name}: {a.latency_ms:.1f}ms[/]")
    else:
        _print(f"\n  [red]Failed[/] to send to [bold]{recipient}[/]")
        for a in report.attempts:
            _print(f"    [red]{a.transport_name}: {a.error}[/]")
        sys.exit(1)
    _print("")


@main.command()
@click.option("--config", "-c", default=None, help="Config file path.")
@click.option("--json-out", is_flag=True, help="Output as JSON.")
def receive(config: Optional[str], json_out: bool):
    """Check all transports for incoming messages.

    Polls every configured transport, deduplicates, and
    displays received messages.
    """
    from .core import SKComm

    comm = SKComm.from_config(config)
    envelopes = comm.receive()

    if not envelopes:
        _print("\n  [dim]No new messages.[/]\n")
        return

    if json_out:
        for env in envelopes:
            click.echo(env.model_dump_json(indent=2))
        return

    _print(f"\n  [bold]{len(envelopes)}[/] message(s) received:\n")

    if console:
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("From", style="cyan")
        table.add_column("Type", style="dim")
        table.add_column("Content", max_width=60)
        table.add_column("Thread", style="dim", max_width=12)
        table.add_column("Urgency")

        urgency_colors = {
            "low": "dim",
            "normal": "white",
            "high": "yellow",
            "critical": "bold red",
        }

        for env in envelopes:
            preview = env.payload.content[:80] + ("..." if len(env.payload.content) > 80 else "")
            urg = env.metadata.urgency.value
            urg_color = urgency_colors.get(urg, "white")
            tid = env.metadata.thread_id[:12] if env.metadata.thread_id else ""
            table.add_row(
                env.sender,
                env.payload.content_type.value,
                preview,
                tid,
                f"[{urg_color}]{urg.upper()}[/]",
            )

        console.print(table)
    else:
        for env in envelopes:
            click.echo(f"  {env.sender} [{env.payload.content_type.value}]: {env.payload.content[:80]}")

    _print("")


@main.command()
@click.option("--config", "-c", default=None, help="Config file path.")
@click.option("--json-out", is_flag=True, help="Output as JSON.")
def status(config: Optional[str], json_out: bool):
    """Show SKComm status and transport health."""
    from .core import SKComm

    comm = SKComm.from_config(config)
    st = comm.status()

    if json_out:
        click.echo(json.dumps(st, indent=2, default=str))
        return

    ident = st["identity"]
    _print("")
    if console:
        console.print(
            Panel(
                f"Identity: [bold cyan]{ident.get('name', 'unknown')}[/]\n"
                f"Fingerprint: {ident.get('fingerprint') or '[dim]none[/]'}\n"
                f"Mode: [bold]{st['default_mode']}[/]\n"
                f"Encrypt: {'[green]yes[/]' if st['encrypt'] else '[red]no[/]'}\n"
                f"Sign: {'[green]yes[/]' if st['sign'] else '[red]no[/]'}\n"
                f"Transports: [bold]{st['transport_count']}[/]",
                title="SKComm",
                border_style="bright_blue",
            )
        )

    transports = st.get("transports", {})
    if transports and console:
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Transport", style="bold")
        table.add_column("Status")
        table.add_column("Latency", justify="right")
        table.add_column("Details", style="dim")

        status_colors = {
            "available": "green",
            "degraded": "yellow",
            "unavailable": "red",
        }

        for name, health in transports.items():
            if isinstance(health, dict):
                s = health.get("status", "unknown")
                color = status_colors.get(s, "dim")
                lat = f"{health.get('latency_ms', 0):.1f}ms" if health.get("latency_ms") else ""
                err = health.get("error", "")
                table.add_row(name, f"[{color}]{s.upper()}[/]", lat, err)

        console.print(table)

    _print("")


@main.command("init")
@click.option("--name", prompt="Agent name", help="Your agent identity name.")
@click.option("--fingerprint", default=None, help="PGP fingerprint for signing.")
def init_config(name: str, fingerprint: Optional[str]):
    """Initialize SKComm configuration.

    Creates ~/.skcomm/config.yml with sensible defaults
    and registers the Syncthing and file transports.
    """
    import yaml

    home = Path(_HOME).expanduser()
    home.mkdir(parents=True, exist_ok=True)

    config_path = home / "config.yml"
    if config_path.exists():
        if not click.confirm(f"Config already exists at {config_path}. Overwrite?", default=False):
            _print("[yellow]Aborted.[/]")
            return

    config = {
        "skcomm": {
            "version": "1.0.0",
            "identity": {"name": name},
            "defaults": {
                "mode": "failover",
                "encrypt": True,
                "sign": True,
                "ack": True,
                "retry_max": 5,
                "ttl": 86400,
            },
            "transports": {
                "syncthing": {
                    "enabled": True,
                    "priority": 1,
                    "settings": {
                        "comms_root": str(Path("~/.skcapstone/comms")),
                    },
                },
                "file": {
                    "enabled": True,
                    "priority": 2,
                    "settings": {
                        "drop_root": str(home / "filedrop"),
                    },
                },
            },
        }
    }

    if fingerprint:
        config["skcomm"]["identity"]["fingerprint"] = fingerprint

    config_path.write_text(yaml.dump(config, default_flow_style=False))

    (home / "logs").mkdir(exist_ok=True)
    (home / "filedrop" / "inbox").mkdir(parents=True, exist_ok=True)
    (home / "filedrop" / "outbox").mkdir(parents=True, exist_ok=True)

    _print(f"\n  [green]SKComm initialized[/]")
    _print(f"  Config: [cyan]{config_path}[/]")
    _print(f"  Identity: [bold]{name}[/]")
    _print(f"  Transports: syncthing (priority 1), file (priority 2)")
    _print("")


@main.command("peers")
@click.option("--config", "-c", default=None, help="Config file path.")
@click.option("--json-out", is_flag=True, help="Output as JSON.")
def peers(config: Optional[str], json_out: bool):
    """List known peers from the peer store.

    Shows all peers discovered via `skcomm discover` or added
    manually, with their transport endpoints and last-seen times.
    """
    from .discovery import PeerStore

    store = PeerStore()
    all_peers = store.list_all()

    if json_out:
        import json as _json

        click.echo(_json.dumps(
            [p.model_dump(mode="json", exclude_none=True) for p in all_peers],
            indent=2,
        ))
        return

    if not all_peers:
        _print("\n  [dim]No peers in store.[/]")
        _print("  [dim]Run [bold]skcomm discover[/] to scan for peers.[/]\n")
        return

    _print(f"\n  [bold]{len(all_peers)}[/] peer(s):\n")

    if console:
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Name", style="cyan")
        table.add_column("Transports", style="dim")
        table.add_column("Via", style="dim")
        table.add_column("Last Seen")
        table.add_column("Fingerprint", style="dim", max_width=16)

        for p in all_peers:
            transports = ", ".join(t.transport for t in p.transports) or "-"
            seen = p.last_seen.strftime("%Y-%m-%d %H:%M") if p.last_seen else "-"
            fp = (p.fingerprint[:16] + "...") if p.fingerprint and len(p.fingerprint) > 16 else (p.fingerprint or "-")
            table.add_row(p.name, transports, p.discovered_via, seen, fp)

        console.print(table)
    else:
        for p in all_peers:
            transports = ", ".join(t.transport for t in p.transports) or "none"
            click.echo(f"  {p.name}  [{transports}]  via {p.discovered_via}")

    _print("")


@main.command("discover")
@click.option("--config", "-c", default=None, help="Config file path.")
@click.option("--save/--no-save", default=True, help="Save to peer store.")
@click.option("--mdns/--no-mdns", default=False, help="Include mDNS LAN scan.")
@click.option("--json-out", is_flag=True, help="Output as JSON.")
def discover(config: Optional[str], save: bool, mdns: bool, json_out: bool):
    """Discover peers on the network and Syncthing mesh.

    Scans Syncthing comms directories, file transport inboxes,
    and optionally the local network via mDNS. Discovered peers
    are saved to the peer store for use by the router.

    Examples:

        skcomm discover

        skcomm discover --mdns

        skcomm discover --json-out
    """
    from .discovery import PeerStore, discover_all

    peers_found = discover_all(skip_mdns=not mdns)

    if json_out:
        import json as _json

        click.echo(_json.dumps(
            [p.model_dump(mode="json", exclude_none=True) for p in peers_found],
            indent=2,
        ))
        if save:
            store = PeerStore()
            for p in peers_found:
                store.add(p)
        return

    if not peers_found:
        _print("\n  [dim]No peers discovered.[/]")
        _print("  [dim]Ensure Syncthing is running or send a message first.[/]\n")
        return

    _print(f"\n  [bold]{len(peers_found)}[/] peer(s) discovered:\n")

    if console:
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Name", style="cyan")
        table.add_column("Transports", style="dim")
        table.add_column("Via", style="dim")
        table.add_column("Last Seen")

        for p in peers_found:
            transports = ", ".join(t.transport for t in p.transports) or "-"
            seen = p.last_seen.strftime("%Y-%m-%d %H:%M") if p.last_seen else "-"
            table.add_row(p.name, transports, p.discovered_via, seen)

        console.print(table)
    else:
        for p in peers_found:
            transports = ", ".join(t.transport for t in p.transports) or "none"
            click.echo(f"  {p.name}  [{transports}]  via {p.discovered_via}")

    if save:
        store = PeerStore()
        for p in peers_found:
            store.add(p)
        _print(f"  [green]Saved to {store.peers_dir}[/]\n")
    else:
        _print("")


@main.command("heartbeat")
@click.option("--config", "-c", default=None, help="Config file path.")
@click.option("--emit/--no-emit", default=True, help="Emit our heartbeat first.")
@click.option("--json-out", is_flag=True, help="Output as JSON.")
def heartbeat(config: Optional[str], emit: bool, json_out: bool):
    """Emit a heartbeat and show peer liveness.

    Writes a heartbeat file to the shared comms directory
    (propagated by Syncthing) and scans for peer heartbeats.

    Examples:

        skcomm heartbeat

        skcomm heartbeat --no-emit

        skcomm heartbeat --json-out
    """
    from .config import load_config
    from .heartbeat import HeartbeatMonitor, PeerLiveness

    cfg = load_config(config)
    monitor = HeartbeatMonitor(
        agent_name=cfg.identity.name,
        fingerprint=cfg.identity.fingerprint,
        transports=[
            name for name, tc in cfg.transports.items() if tc.enabled
        ],
    )

    if emit:
        monitor.emit()

    results = monitor.scan()

    if json_out:
        import json as _json

        click.echo(_json.dumps(
            [r.model_dump(mode="json", exclude_none=True) for r in results],
            indent=2,
        ))
        return

    if not results:
        if emit:
            _print(f"\n  [green]Heartbeat emitted[/] as [bold]{cfg.identity.name}[/]")
        _print("  [dim]No peer heartbeats found yet.[/]\n")
        return

    if emit:
        _print(f"\n  [green]Heartbeat emitted[/] as [bold]{cfg.identity.name}[/]\n")
    else:
        _print("")

    status_styles = {
        PeerLiveness.ALIVE: "green",
        PeerLiveness.STALE: "yellow",
        PeerLiveness.DEAD: "red",
        PeerLiveness.UNKNOWN: "dim",
    }

    if console:
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Peer", style="cyan")
        table.add_column("Status")
        table.add_column("Age", justify="right")
        table.add_column("Transports", style="dim")

        for r in results:
            color = status_styles.get(r.status, "dim")
            age = f"{int(r.age_seconds)}s" if r.age_seconds is not None else "-"
            transports = ", ".join(r.transports) or "-"
            table.add_row(
                r.name,
                f"[{color}]{r.status.value.upper()}[/{color}]",
                age,
                transports,
            )

        console.print(table)
    else:
        for r in results:
            age = f"{int(r.age_seconds)}s" if r.age_seconds is not None else "?"
            click.echo(f"  {r.name:16} {r.status.value:8} {age}")

    alive = sum(1 for r in results if r.status == PeerLiveness.ALIVE)
    _print(f"\n  {alive}/{len(results)} peers alive\n")


# ---------------------------------------------------------------------------
# SKWorld marketplace commands
# ---------------------------------------------------------------------------


@main.group("skill")
def skill_group():
    """SKWorld marketplace — publish and discover agent skills.

    Browse, publish, and install sovereign agent skills via
    the Nostr-based marketplace.
    """


@skill_group.command("list")
@click.option("--json-out", is_flag=True, help="Output as JSON.")
def skill_list(json_out: bool):
    """List locally installed skills."""
    from .marketplace import SkillRegistry

    reg = SkillRegistry()
    skills = reg.list_all()

    if json_out:
        import json as _json

        click.echo(_json.dumps(
            [s.model_dump(mode="json", exclude_none=True) for s in skills],
            indent=2,
        ))
        return

    if not skills:
        _print("\n  [dim]No skills installed.[/]")
        _print("  [dim]Run [bold]skcomm skill search[/] to browse the marketplace.[/]\n")
        return

    _print(f"\n  [bold]{len(skills)}[/] skill(s) installed:\n")

    if console:
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Name", style="cyan")
        table.add_column("Version")
        table.add_column("Author", style="dim")
        table.add_column("Tags", style="dim")

        for s in skills:
            table.add_row(s.name, s.version, s.author or "-", ", ".join(s.tags) or "-")

        console.print(table)
    else:
        for s in skills:
            click.echo(f"  {s.name:24} v{s.version:8} {s.author or '-'}")

    _print("")


@skill_group.command("search")
@click.argument("query", required=False, default=None)
@click.option("--relay", "-r", multiple=True, help="Override relay URLs.")
@click.option("--json-out", is_flag=True, help="Output as JSON.")
def skill_search(query: Optional[str], relay: tuple, json_out: bool):
    """Search the Nostr marketplace for skills.

    Queries configured relays for published skill manifests.

    Examples:

        skcomm skill search

        skcomm skill search security

        skcomm skill search email --json-out
    """
    from .marketplace import search_skills

    relays = list(relay) if relay else None
    results = search_skills(query=query, relays=relays)

    if json_out:
        import json as _json

        click.echo(_json.dumps(
            [s.model_dump(mode="json", exclude_none=True) for s in results],
            indent=2,
        ))
        return

    if not results:
        _print("\n  [dim]No skills found.[/]\n")
        return

    _print(f"\n  [bold]{len(results)}[/] skill(s) found:\n")

    if console:
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Name", style="cyan")
        table.add_column("Version")
        table.add_column("Author", style="dim")
        table.add_column("Description", max_width=40)
        table.add_column("Tags", style="dim")

        for s in results:
            desc = (s.description[:37] + "...") if len(s.description) > 40 else s.description
            table.add_row(s.name, s.version, s.author or "-", desc, ", ".join(s.tags) or "-")

        console.print(table)
    else:
        for s in results:
            click.echo(f"  {s.name:24} v{s.version:8} {s.description[:50]}")

    _print("")


@skill_group.command("publish")
@click.argument("manifest_path", type=click.Path(exists=True))
@click.option("--key", envvar="NOSTR_PRIVATE_KEY", help="Nostr private key hex (or NOSTR_PRIVATE_KEY env).")
@click.option("--relay", "-r", multiple=True, help="Override relay URLs.")
def skill_publish(manifest_path: str, key: Optional[str], relay: tuple):
    """Publish a skill manifest to the Nostr marketplace.

    Reads a YAML manifest file and publishes it as a Nostr
    event to configured relays.

    Examples:

        skcomm skill publish skill.yml --key $NOSTR_KEY

        NOSTR_PRIVATE_KEY=abc... skcomm skill publish skill.yml
    """
    from .marketplace import SkillManifest, publish_skill

    if not key:
        _print("\n  [red]Error:[/] Nostr private key required.")
        _print("  Set --key or NOSTR_PRIVATE_KEY env var.\n")
        raise SystemExit(1)

    manifest = SkillManifest.from_yaml_file(Path(manifest_path))
    relays = list(relay) if relay else None
    event_id = publish_skill(manifest, key, relays=relays)

    if event_id:
        _print(f"\n  [green]Published[/] [bold]{manifest.name}[/] v{manifest.version}")
        _print(f"  Event: [dim]{event_id}[/]\n")
    else:
        _print(f"\n  [red]Failed[/] to publish {manifest.name}.\n")
        raise SystemExit(1)


@skill_group.command("install")
@click.argument("name")
@click.option("--relay", "-r", multiple=True, help="Override relay URLs.")
def skill_install(name: str, relay: tuple):
    """Install a skill from the Nostr marketplace.

    Searches for the skill by name, downloads the manifest,
    and adds it to the local skill registry.

    Examples:

        skcomm skill install email-prescreening
    """
    from .marketplace import SkillRegistry, search_skills

    _print(f"\n  Searching for [bold]{name}[/]...")
    relays = list(relay) if relay else None
    results = search_skills(query=name, relays=relays)

    match = next((s for s in results if s.name == name), None)
    if not match and results:
        match = results[0]

    if not match:
        _print(f"  [red]Not found:[/] {name}\n")
        raise SystemExit(1)

    reg = SkillRegistry()
    reg.install(match)
    _print(f"  [green]Installed[/] [bold]{match.name}[/] v{match.version}")
    if match.install_cmd:
        _print(f"  Run: [cyan]{match.install_cmd}[/]")
    _print("")


# ---------------------------------------------------------------------------
# Queue commands
# ---------------------------------------------------------------------------


@main.group("queue")
def queue_group():
    """Message queue — manage undeliverable envelopes.

    View, drain, and purge the persistent outbox queue
    for messages that couldn't be delivered.
    """


@queue_group.command("list")
@click.option("--json-out", is_flag=True, help="Output as JSON.")
def queue_list(json_out: bool):
    """List all queued envelopes."""
    from .queue import MessageQueue

    q = MessageQueue()
    items = q.list_all()

    if json_out:
        import json as _json

        click.echo(_json.dumps(
            [m.model_dump(mode="json", exclude_none=True) for m in items],
            indent=2,
        ))
        return

    if not items:
        _print("\n  [dim]Queue is empty.[/]\n")
        return

    _print(f"\n  [bold]{len(items)}[/] envelope(s) queued:\n")

    if console:
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("ID", style="cyan", max_width=12)
        table.add_column("Recipient")
        table.add_column("Attempts", justify="right")
        table.add_column("Queued")
        table.add_column("Status")

        for m in items:
            eid = m.envelope_id[:12]
            queued = m.queued_at.strftime("%H:%M:%S") if m.queued_at else "-"
            if m.is_expired:
                status = "[red]EXPIRED[/]"
            elif m.is_ready:
                status = "[green]READY[/]"
            else:
                status = "[yellow]WAITING[/]"
            table.add_row(eid, m.recipient, str(m.attempts), queued, status)

        console.print(table)
    else:
        for m in items:
            click.echo(f"  {m.envelope_id[:12]:14} -> {m.recipient:16} attempts={m.attempts}")

    _print("")


@queue_group.command("drain")
@click.option("--config", "-c", default=None, help="Config file path.")
def queue_drain(config: Optional[str]):
    """Attempt to deliver all pending queued envelopes.

    Retries each ready envelope through the configured transports.
    Successfully delivered envelopes are removed from the queue.
    """
    from .core import SKComm
    from .queue import MessageQueue

    comm = SKComm.from_config(config)
    q = MessageQueue()

    if q.size == 0:
        _print("\n  [dim]Queue is empty — nothing to drain.[/]\n")
        return

    _print(f"\n  Draining {q.size} envelope(s)...\n")

    def try_send(envelope_bytes: bytes, recipient: str) -> bool:
        from .models import MessageEnvelope
        try:
            envelope = MessageEnvelope.from_bytes(envelope_bytes)
            report = comm.send_envelope(envelope)
            return report.delivered
        except Exception:
            return False

    delivered, failed = q.drain(try_send)
    _print(f"  [green]{delivered}[/] delivered, [red]{failed}[/] failed, [dim]{q.size}[/] remaining\n")


@queue_group.command("purge")
@click.option("--expired", is_flag=True, default=False, help="Only purge expired envelopes.")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
def queue_purge(expired: bool, yes: bool):
    """Remove envelopes from the queue.

    By default, removes ALL queued envelopes. Use --expired
    to only remove envelopes that have exceeded their TTL.
    """
    from .queue import MessageQueue

    q = MessageQueue()

    if q.size == 0:
        _print("\n  [dim]Queue is empty.[/]\n")
        return

    if expired:
        removed = q.purge_expired()
        _print(f"\n  Purged [bold]{removed}[/] expired envelope(s). {q.size} remaining.\n")
    else:
        if not yes:
            if not click.confirm(f"  Remove all {q.size} queued envelopes?", default=False):
                _print("  [dim]Cancelled.[/]\n")
                return
        items = q.list_all()
        for m in items:
            q.dequeue(m.envelope_id)
        _print(f"\n  Purged [bold]{len(items)}[/] envelope(s).\n")


@main.command("stats")
@click.option("--json-out", is_flag=True, help="Output as JSON.")
@click.option("--reset", is_flag=True, help="Reset all metrics.")
def stats_cmd(json_out: bool, reset: bool):
    """Show per-transport delivery metrics.

    Displays success/failure counts, latency, and error
    history for each transport.

    Examples:

        skcomm stats

        skcomm stats --json-out

        skcomm stats --reset
    """
    from .metrics import MetricsCollector

    mc = MetricsCollector()

    if reset:
        mc.reset()
        _print("\n  [green]Metrics reset.[/]\n")
        return

    if json_out:
        import json as _json

        click.echo(_json.dumps(mc.summary(), indent=2, default=str))
        return

    all_stats = mc.all_stats()
    if not all_stats:
        _print("\n  [dim]No transport metrics yet.[/]")
        _print("  [dim]Send or receive a message to start tracking.[/]\n")
        return

    summary = mc.summary()
    _print(
        f"\n  [bold]Transport Metrics[/]  "
        f"[green]{summary['total_sends_ok']}[/] sent  "
        f"[red]{summary['total_sends_fail']}[/] failed  "
        f"[cyan]{summary['total_receives']}[/] received  "
        f"({summary['overall_success_rate']} success)\n"
    )

    if console:
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Transport", style="cyan")
        table.add_column("Sent", justify="right")
        table.add_column("Failed", justify="right")
        table.add_column("Recv", justify="right")
        table.add_column("Rate")
        table.add_column("Avg Latency", justify="right")
        table.add_column("Last Error", style="dim", max_width=30)

        for s in all_stats:
            rate_color = "green" if s.success_rate >= 90 else "yellow" if s.success_rate >= 50 else "red"
            avg = f"{s.avg_latency_ms:.1f}ms" if s.avg_latency_ms > 0 else "-"
            err = (s.last_error[:27] + "...") if s.last_error and len(s.last_error) > 30 else (s.last_error or "-")
            table.add_row(
                s.transport,
                str(s.sends_ok),
                str(s.sends_fail),
                str(s.receives),
                f"[{rate_color}]{s.success_rate:.0f}%[/{rate_color}]",
                avg,
                err,
            )

        console.print(table)
    else:
        for s in all_stats:
            click.echo(
                f"  {s.transport:16} ok={s.sends_ok} fail={s.sends_fail} "
                f"recv={s.receives} rate={s.success_rate:.0f}%"
            )

    _print("")


if __name__ == "__main__":
    main()
