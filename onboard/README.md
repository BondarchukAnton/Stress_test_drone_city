# On-drone deployment (Pi-5 + Docker)

This stack is built so a drone/rover is just **one more node** that joins the
swarm over the network. Two ways to run the brain on a drone, sharing the exact
same robot interface (the §6 bridge contract) and the same hub:

```
                 orchestrator host                     each robot host
  ┌───────────────────────────────────┐      ┌──────────────────────────────┐
  │  hub (dashboard + HTTP API + SSE)  │◀────▶│  agent  ──HTTP──▶ bridge      │
  │  coordinator (owns world model)    │ HTTP │  (HttpBoard → hub)  (camera/  │
  └───────────────────────────────────┘      │                     Nav2)     │
        ▲  drones stream their thinking here  └──────────────────────────────┘
```

## A. The Python agent-loop wrapper (what runs today)

```bash
# on the orchestrator:
HUB_TOKEN=$(openssl rand -hex 16) docker compose -f docker-compose.hub.yml up -d

# on each drone (has Docker):
AGENT_ID=drone-5 ROLE=scout \
HUB_URL=http://<orchestrator>:8080 HUB_TOKEN=<same> \
SVERK_API_KEY=<key> \
docker compose -p drone-5 -f docker-compose.drone.yml up -d
```

The drone registers with the hub on boot, the coordinator picks it up
automatically (registry discovery), and its streamed reasoning shows up on the
dashboard — click the node to read its full thinking + chat. The bridge stays
local to the robot (mock now; the `rclpy` node in `../bridge/ros2/` on hardware).

## B. Native PicoClaw as the brain (drop-in)

PicoClaw (Go, pin **v0.2.9**) adds tools declaratively via **MCP servers — no Go
recompile**. So PicoClaw runs the agent and reaches the robot through one generic
MCP tool server that proxies to the same bridge HTTP contract:

- `picoclaw_bridge_mcp.py` — the `call_bridge` shim: exposes `photograph`,
  `detect_obstacle`, `navigate`, `get_pose` as MCP tools, each proxying to
  `BRIDGE_URL` (§6). Only those four actions exist — no shell/file escape (§11).
- `picoclaw.config.json` — skeleton `~/.picoclaw/config.json`: `model_list`
  points at the shared `sverk/qwen35` gateway (keys in `.security.yml`, never in
  config), with an on-board Ollama fallback; `tools.mcp.servers.bridge` launches
  the shim.

To stream PicoClaw's thinking to the central dashboard, have the wrapper (or a
small sidecar) POST PicoClaw's per-step reasoning to the hub `POST /events` as
`thought_start`/`thought_delta`/`thought_end` for that `AGENT_ID` — the same
events the dashboard already renders and persists per agent. (PicoClaw's
streaming callback / `--verbose` reasoning is the source; pipe it to the hub.)

> Pin the PicoClaw commit and check the binary into your image; verify the MCP
> transport framing matches your build (the shim uses newline-delimited
> JSON-RPC; switch to Content-Length framing if needed). The bridge contract and
> the hub API do not change either way.

## Models

- **Default: shared server model** (`MODEL_PROVIDER=sverk`, `qwen35`). Every
  drone calls it independently over the network.
- **On-board local model** (offline / no link): `MODEL_PROVIDER=ollama`,
  `OLLAMA_BASE=http://host.docker.internal:11434`, a 1–3B Q4 model on 4GB units
  (expect quality/latency hits, brief §10). Either way the drone streams its
  thoughts to the hub the same way.

## Networking & safety (§11)

Put all robots + the orchestrator on one private LAN/VPN; drones reach the hub at
`HUB_URL`. Set a shared `HUB_TOKEN` so only your robots can post. On hardware the
bridge gets a hard **e-stop** before `navigate` touches a physical rover, and the
coordinator's `world.ready` certification stays the gate (brief §5/§11).

## Adding/removing drones

Plug-and-play: bring a drone node up → it registers → the coordinator includes
it next run. Stopping a node drops it from the registry on the next clean run.
(Mapping an arbitrary number of drones onto map sectors — dynamic N-way
decomposition — is the open TODO; today extra scouts beyond the 4 sectors get
redundant assignments.)
