#!/usr/bin/env python3
"""PicoClaw <-> robot bridge: a generic MCP tool server (the brief §0/§12
`call_bridge` shim).

PicoClaw adds tools declaratively via MCP servers spawned as child processes --
no Go recompile (verified against sipeed/picoclaw v0.2.9). This stdio MCP server
exposes the robot's whitelisted bridge actions as MCP tools and proxies each
call to the bridge HTTP sidecar (bridge/mock.py in dev, the rclpy node on
hardware). So PicoClaw's *agent* gains robot tools while the bridge contract
(§6) stays the single seam between sim and hardware.

Wire it into ~/.picoclaw/config.json under tools.mcp.servers (see
picoclaw.config.json). Set BRIDGE_URL to the local bridge.

NOTE: this is a SKELETON to adapt to your pinned PicoClaw MCP transport. It
implements newline-delimited JSON-RPC 2.0 (initialize / tools/list /
tools/call). If your PicoClaw build uses LSP-style Content-Length framing,
swap _read_msg/_write_msg accordingly. The valuable, correct part -- the tool
schemas and the HTTP proxy to the §6 contract -- is transport-independent.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://localhost:9000").rstrip("/")
# hub (blackboard-over-HTTP) — the same endpoints HttpBoard uses (agent/bb.py).
# With HUB_URL set, PicoClaw gains BOARD tools: it reads the shared world/chat,
# posts messages, reports progress and streams its reasoning — a full citizen of
# the multi-agent dispute, not just a flight executor. Without HUB_URL those
# tools answer with a clear error instead of silently no-oping.
HUB_URL = (os.environ.get("HUB_URL") or "").rstrip("/")
HUB_TOKEN = os.environ.get("HUB_TOKEN", "")
AGENT_ID = os.environ.get("AGENT_ID", "picoclaw")

TOOLS = [
    {"name": "photograph",
     "description": "Photograph a street sector; returns the image path.",
     "inputSchema": {"type": "object", "properties": {"sector": {"type": "string"}},
                     "required": ["sector"]}},
    {"name": "detect_obstacle",
     "description": "Detect obstacles in a captured image; returns obstacles + coverage.",
     "inputSchema": {"type": "object", "properties": {"image_path": {"type": "string"}},
                     "required": ["image_path"]}},
    {"name": "navigate",
     "description": "Drive from A to B over an occupancy grid; returns arrival status.",
     "inputSchema": {"type": "object",
                     "properties": {"from": {"type": "array"}, "to": {"type": "array"},
                                    "grid": {"type": "array"}},
                     "required": ["from", "to", "grid"]}},
    {"name": "get_pose",
     "description": "Current robot pose {xy, heading}.",
     "inputSchema": {"type": "object", "properties": {}}},
    # survey task (поиск груза): полёт по клеткам + анализ клетки
    {"name": "fly_to",
     "description": "Fly to a grid cell [x,y] (multi-cell hops allowed).",
     "inputSchema": {"type": "object",
                     "properties": {"cell": {"type": "array",
                                             "items": {"type": "integer"},
                                             "minItems": 2, "maxItems": 2}},
                     "required": ["cell"]}},
    {"name": "photograph_cell",
     "description": "Photograph the given grid cell top-down; returns image path.",
     "inputSchema": {"type": "object",
                     "properties": {"cell": {"type": "array",
                                             "items": {"type": "integer"},
                                             "minItems": 2, "maxItems": 2}},
                     "required": ["cell"]}},
    {"name": "analyze",
     "description": "Analyze a cell for cargo; close_look=true is the low-pass verification.",
     "inputSchema": {"type": "object",
                     "properties": {"cell": {"type": "array",
                                             "items": {"type": "integer"},
                                             "minItems": 2, "maxItems": 2},
                                    "close_look": {"type": "boolean"}},
                     "required": ["cell"]}},
    # пауза / замена аккумуляторов
    {"name": "land",
     "description": "Land the drone (pause / battery swap).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "takeoff",
     "description": "Take off after a pause; pose re-syncs to real telemetry.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "pause",
     "description": "Battery-swap / kill-switch prep: land and freeze until takeoff.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "set_cell",
     "description": "Arbitrary start / kill-switch recovery: assert which grid cell "
                    "[x,y] the drone is actually on (used until aruco re-localises).",
     "inputSchema": {"type": "object",
                     "properties": {"cell": {"type": "array",
                                             "items": {"type": "integer"},
                                             "minItems": 2, "maxItems": 2}},
                     "required": ["cell"]}},
    # доска (hub): участие в мультиагентном чате/координации, не только полёт
    {"name": "read_board",
     "description": "Read the shared board: phase, world state and the last chat "
                    "messages. Call this each cycle before deciding what to do.",
     "inputSchema": {"type": "object",
                     "properties": {"last": {"type": "integer",
                                             "description": "how many recent messages"}}}},
    {"name": "post_message",
     "description": "Post a chat message to the board (type: CLAIM/ROUTE/REBUTTAL/"
                    "STATUS/OBSERVATION/..., to: 'all' or an agent id).",
     "inputSchema": {"type": "object",
                     "properties": {"type": {"type": "string"},
                                    "to": {"type": "string"},
                                    "body": {"type": "string"},
                                    "payload": {"type": "object"}},
                     "required": ["type", "to", "body"]}},
    {"name": "report_progress",
     "description": "Write this agent's progress record (cell, covered, status...) "
                    "so the coordinator and the dashboard see it.",
     "inputSchema": {"type": "object",
                     "properties": {"progress": {"type": "object"}},
                     "required": ["progress"]}},
    {"name": "emit_thought",
     "description": "Stream one line of your reasoning to the live dashboard chat.",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
]


def _http(method: str, path: str, body=None, stream=False, base=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json"}
    if base is not None and HUB_TOKEN:            # hub POSTs need the team token
        headers["authorization"] = f"Bearer {HUB_TOKEN}"
    req = urllib.request.Request((base if base is not None else BRIDGE_URL) + path,
                                 data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=120) as resp:
        if stream:  # navigate streams ndjson; return the final status frame
            last = {}
            for line in resp:
                line = line.strip()
                if line:
                    last = json.loads(line.decode())
            return last
        return json.loads(resp.read().decode())


def _hub(method: str, path: str, body=None):
    if not HUB_URL:
        raise ValueError("board tools need HUB_URL (the shared-board hub) in env")
    return _http(method, path, body, base=HUB_URL)


def call_tool(name: str, args: dict) -> dict:
    """Proxy a tool call to the whitelisted bridge endpoint (§6). Strict: only
    these four actions exist -- no shell/file escape (§11)."""
    if name == "photograph":
        return _http("POST", "/photograph", {"sector": args["sector"]})
    if name == "detect_obstacle":
        return _http("POST", "/detect_obstacle", {"image_path": args["image_path"]})
    if name == "navigate":
        return _http("POST", "/navigate",
                     {"from": args["from"], "to": args["to"], "grid": args["grid"]},
                     stream=True)
    if name == "get_pose":
        return _http("GET", "/pose")
    if name == "fly_to":
        return _http("POST", "/move", {"to": args["cell"]})
    if name == "photograph_cell":
        return _http("POST", "/photograph", {"cell": args["cell"]})
    if name == "analyze":
        return _http("POST", "/analyze",
                     {"cell": args["cell"], "close_look": bool(args.get("close_look"))})
    if name == "land":
        return _http("POST", "/land", {})
    if name == "takeoff":
        return _http("POST", "/takeoff", {})
    if name == "pause":
        return _http("POST", "/pause", {})
    if name == "set_cell":
        return _http("POST", "/set_cell", {"cell": args["cell"]})
    # --- board (hub) tools: the multi-agent side ---------------------------
    if name == "read_board":
        last = int(args.get("last") or 12)
        msgs = _hub("GET", "/messages") or []
        return {"phase": _hub("GET", "/state/phase"),
                "world": _hub("GET", "/state/world"),
                "messages": msgs[-last:]}
    if name == "post_message":
        msg = {"from": AGENT_ID, "to": args["to"], "type": args["type"],
               "body": args["body"], "payload": args.get("payload") or {}}
        return _hub("POST", "/messages", msg)
    if name == "report_progress":
        _hub("POST", f"/progress/{AGENT_ID}", dict(args["progress"]))
        return {"ok": True}
    if name == "emit_thought":
        _hub("POST", "/events", {"kind": "thought", "from": AGENT_ID,
                                 "text": str(args["text"])[:800]})
        return {"ok": True}
    raise ValueError(f"unknown tool {name}")


# ---- minimal newline-delimited JSON-RPC 2.0 over stdio --------------------
def _write(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params", {})
        try:
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05",
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "picoclaw-bridge", "version": "0.1.0"}}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                out = call_tool(params["name"], params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(out)}]}
            elif method in ("notifications/initialized", "ping"):
                continue
            else:
                _write({"jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32601, "message": f"method {method}"}})
                continue
            _write({"jsonrpc": "2.0", "id": mid, "result": result})
        except Exception as exc:  # strict boundary: report, never crash
            _write({"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32000, "message": str(exc)}})


if __name__ == "__main__":
    main()
