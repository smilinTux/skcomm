/**
 * SKComm — OpenClaw Plugin
 *
 * Registers agent tools that wrap the skcomm CLI so Lumina and other
 * OpenClaw agents can call P2P communication operations as first-class tools.
 *
 * Requires: skcomm CLI on PATH (typically via ~/.skenv/bin/skcomm)
 */

import { execSync } from "node:child_process";
import type { OpenClawPluginApi, AnyAgentTool } from "openclaw/plugin-sdk";
import { emptyPluginConfigSchema } from "openclaw/plugin-sdk";

const SKCOMM_BIN = process.env.SKCOMM_BIN || "skcomm";
const EXEC_TIMEOUT = 30_000;

function runCli(args: string): { ok: boolean; output: string } {
  try {
    const raw = execSync(`${SKCOMM_BIN} ${args}`, {
      encoding: "utf-8",
      timeout: EXEC_TIMEOUT,
      env: {
        ...process.env,
        PATH: `${process.env.HOME}/.local/bin:${process.env.HOME}/.skenv/bin:${process.env.PATH}`,
      },
    }).trim();
    return { ok: true, output: raw };
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    return { ok: false, output: msg };
  }
}

function textResult(text: string) {
  return { content: [{ type: "text" as const, text }] };
}

function escapeShellArg(s: string): string {
  return `'${s.replace(/'/g, "'\\''")}'`;
}

// ── Tool definitions ────────────────────────────────────────────────────

function createSKCommSendTool() {
  return {
    name: "skcomm_send",
    label: "SKComm Send",
    description:
      "Send a P2P message to a peer via all available transports (Syncthing, mDNS, etc.).",
    parameters: {
      type: "object",
      required: ["to", "message"],
      properties: {
        to: { type: "string", description: "Recipient peer ID or name." },
        message: { type: "string", description: "Message content to send." },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const to = escapeShellArg(String(params.to ?? ""));
      const msg = escapeShellArg(String(params.message ?? ""));
      const result = runCli(`send ${to} ${msg}`);
      return textResult(result.output);
    },
  };
}

function createSKCommReceiveTool() {
  return {
    name: "skcomm_receive",
    label: "SKComm Receive",
    description:
      "Check all transports for incoming messages. Returns any pending messages from peers.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli("receive");
      return textResult(result.output);
    },
  };
}

function createSKCommPeersTool() {
  return {
    name: "skcomm_peers",
    label: "SKComm Peers",
    description:
      "List known peers and their available transports.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli("peers");
      return textResult(result.output);
    },
  };
}

function createSKCommStatusTool() {
  return {
    name: "skcomm_status",
    label: "SKComm Status",
    description:
      "Transport health check — shows status of all communication channels.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli("status");
      return textResult(result.output);
    },
  };
}

function createSKCommDiscoverTool() {
  return {
    name: "skcomm_discover",
    label: "SKComm Discover",
    description:
      "Discover peers via Syncthing and mDNS on the local network.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli("discover");
      return textResult(result.output);
    },
  };
}

// ── Plugin registration ─────────────────────────────────────────────────

const skcommPlugin = {
  id: "skcomm",
  name: "SKComm",
  description:
    "P2P communication layer — send/receive messages, discover peers, and check transport health.",
  configSchema: emptyPluginConfigSchema(),

  register(api: OpenClawPluginApi) {
    const tools = [
      createSKCommSendTool(),
      createSKCommReceiveTool(),
      createSKCommPeersTool(),
      createSKCommStatusTool(),
      createSKCommDiscoverTool(),
    ];

    for (const tool of tools) {
      api.registerTool(tool as unknown as AnyAgentTool, {
        names: [tool.name],
        optional: true,
      });
    }

    api.registerCommand({
      name: "skcomm",
      description: "Run skcomm CLI commands. Usage: /skcomm <subcommand> [args]",
      acceptsArgs: true,
      handler: async (ctx) => {
        const args = ctx.args?.trim() ?? "status";
        const result = runCli(args);
        return { text: result.output };
      },
    });

    api.logger.info?.("SKComm plugin registered (5 tools + /skcomm command)");
  },
};

export default skcommPlugin;
