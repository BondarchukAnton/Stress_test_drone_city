# On-drone & PicoClaw

A drone/rover is just one more node. Two ways to run the brain on it, sharing the
exact same robot interface (the [bridge](bridge.md) contract) and the same
[hub](distributed.md). Files live in `drone/`.

> **2026-07:** унифицированный образ на разные дроны, `HANDLER_ID`/`FLEET`
> (несколько команд в одной сети), привязка в постоянную память Raspberry,
> LED-регистрация на `/fleet` и единая ROS2 bridge-нода (city|painter) поверх
> sverk-ros2 — подробности в [fleet.md](fleet.md); профиль проекта —
> `.env.drone.example`.

## The adaptable drone image (`openclaw-drone`)

One image (`drone/Dockerfile`, multi-arch incl. `linux/arm64` for Pi 4/5) runs
any node in either scenario. Its entrypoint (`drone/entrypoint.sh`) reads the
node's identity from env and adapts:

- `AGENT_ID` / `ROLE` (`scout` | `rover` | `painter`) / `TASK` (`safe_passage`
  | `painting`, inferred from the role if unset) select the behaviour.
- It picks a **bundled SOUL** (`souls/<AGENT_ID>.md`) if present, otherwise
  **generates one** so an arbitrary name like `painter-7` or `drone-9` just
  works. For painters the generated persona's colour + technique are derived
  from the id and overridable with `DRONE_COLOR` / `DRONE_TECHNIQUE` /
  `DRONE_NAME` / `DRONE_SUBJECT`.
- `BRAIN=wrapper` (default) runs the Python §7-loop; `BRAIN=picoclaw` runs the
  native binary if baked in (see B), else falls back to the wrapper.

```bash
# scale a painters swarm: each Pi is one spray-can drone, no file edits
make drone AGENT_ID=painter-5 ROLE=painter TASK=painting HUB_URL=http://<hub>:8080 HUB_TOKEN=<t>
make drone AGENT_ID=painter-6 ROLE=painter TASK=painting HUB_URL=http://<hub>:8080 HUB_TOKEN=<t>
# or smart-rover scouts:
make drone AGENT_ID=drone-5 ROLE=scout HUB_URL=http://<hub>:8080 HUB_TOKEN=<t>
```

`DUMP_SOUL=1` makes the entrypoint print the resolved SOUL + env and exit —
handy for verifying a node's identity before it joins.

## A. The Python agent-loop wrapper (what runs today)

This is `agent/loop.py` with `MODEL_PROVIDER=sverk` (or a local model). It's the
"thin wrapper of the spec §7 loop" the brief describes, with a pluggable brain.
Deploy with `docker-compose.drone.yml` (`make drone ...`). Verified end-to-end.

## B. Native PicoClaw as the brain (drop-in)

PicoClaw (Go, pin **v0.2.9**) adds tools declaratively via **MCP servers spawned
as child processes — no Go recompile** (verified against sipeed/picoclaw). So
PicoClaw runs the agent and reaches the robot through one generic MCP tool server
that proxies to the same bridge HTTP contract.

* **`drone/picoclaw_bridge_mcp.py`** — the `call_bridge` shim: flight tools
  (`fly_to`, `photograph_cell`, `analyze`, `takeoff`, `land`, `get_pose`, …)
  proxying to `BRIDGE_URL`, plus **board tools** (`read_board`, `post_message`,
  `report_progress`, `emit_thought`) proxying to `HUB_URL` with `HUB_TOKEN` —
  so a PicoClaw brain joins the multi-agent chat like any wrapper agent (the
  dashboard cannot tell them apart). No shell/file escape (§11). Stdio JSON-RPC
  2.0 (initialize / tools/list / tools/call). Tested end-to-end without Go:
  `tests/test_picoclaw_shim.py`; protocol prompt: `souls/picoclaw-pilot.md`;
  when to pick which interop — [agent-interop.md](agent-interop.md).
* **`drone/picoclaw.config.json`** — skeleton `~/.picoclaw/config.json`:
  `model_list` points at the shared `sverk/qwen35` gateway (keys in
  `.security.yml`, never in config), with an on-board Ollama fallback;
  `tools.mcp.servers.bridge` launches the shim.

```jsonc
"model_list": [
  { "model_name": "sverk/qwen35",
    "litellm_params": { "model": "openai/qwen35",
      "api_base": "https://ai.sverk.tech/v1", "api_key": "os.environ/SVERK_API_KEY" } }
],
"tools": { "mcp": { "servers": { "bridge": {
  "command": "python", "args": ["/app/drone/picoclaw_bridge_mcp.py"],
  "env": { "BRIDGE_URL": "http://localhost:9000" } } } } }
```

To stream PicoClaw's thinking to the central dashboard, have the wrapper (or a
small sidecar) POST PicoClaw's per-step reasoning to the hub `POST /events` as
`thought_start`/`thought_delta`/`thought_end` for that `AGENT_ID` — the same
events the dashboard already renders and persists per agent. PicoClaw's streaming
callback / verbose reasoning is the source.

> **Caveat (not built/tested here):** no Go toolchain in this environment, and
> this is your pinned PicoClaw version — the shim is a working **skeleton** to
> adapt. Verify the MCP transport framing matches your build (the shim uses
> newline-delimited JSON-RPC; switch to LSP-style Content-Length framing if your
> build needs it). The bridge contract and the hub API don't change either way.
> Pin the PicoClaw commit and check the binary into your image (§11).

## Models

* **Default: shared server model** (`MODEL_PROVIDER=sverk`, `qwen35`). Every drone
  calls it independently over the network. `qwen35` is a reasoning model — its
  chain-of-thought is what the dashboard streams.
* **On-board local model** (offline / no link): `MODEL_PROVIDER=ollama`,
  `OLLAMA_BASE=http://host.docker.internal:11434`, a 1–3B Q4 model on 4 GB units
  (expect quality/latency hits, brief §10). A 2–4 GB Pi can't run a useful LLM,
  so remote is the default; local is the offline fallback. Either way the drone
  streams its thoughts to the hub the same way.

## Pi-5 / ROS2 (designed for, not built — brief §10)

* Per drone on a Pi-5: PicoClaw binary (or the Python wrapper) + the `rclpy`
  bridge node (`bridge/ros2/bridge_node.py`, a stub today) + camera. Keep the §6
  HTTP contract so nothing upstream changes.
* ROS2: target a current LTS distro; the bridge is the only ROS2-aware component.
  GPIO/sensors are extra bridge endpoints with the same HTTP-tool contract.
* The shared-volume blackboard is dev-only; on separate machines it becomes the
  hub's HTTP+SSE API ([distributed](distributed.md)).

## Networking & safety

Put all robots + the orchestrator on one private LAN/VPN; drones reach the hub at
`HUB_URL`. Set a shared `HUB_TOKEN` so only your robots can post. On hardware the
bridge gets a hard **e-stop** before `navigate` touches a physical rover, and the
coordinator's `world.ready` certification stays the gate. See
[security](security.md).

## Adding / removing drones

Plug-and-play: bring a drone node up → it registers → the coordinator includes it
next run. Stopping a node drops it from the registry on the next clean run.
(Mapping an arbitrary number of drones onto sectors is the open TODO — see
[extending](extending.md).)