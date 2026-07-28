#!/usr/bin/env sh
# Drone-node entrypoint (Pi-5 / any Docker host).
#
# Adapts ONE image to whatever drone it is plugged into: pick the role + task +
# identity from env, make sure a SOUL exists (bundled, or generated on the fly
# so an arbitrary drone name like painter-7 just works), then launch the brain.
#
# Env it reads:
#   AGENT_ID   drone id, e.g. drone-5 / rover / painter-7        (required)
#   ROLE       scout | rover | painter                           (required)
#   TASK       safe_passage | painting        (default: inferred from ROLE)
#   HUB_URL    orchestrator hub, e.g. http://10.0.0.2:8080       (required to join)
#   BRIDGE_URL local robot bridge              (default http://localhost:9000)
#   BRAIN      wrapper | picoclaw              (default: wrapper)
#   DRONE_NAME / DRONE_COLOR / DRONE_TECHNIQUE / DRONE_SUBJECT
#              optional persona overrides for a generated painter soul
set -eu

# AGENT_ID можно не задавать: возьмём из сохранённой привязки (постоянная
# память Raspberry, BIND_FILE) — дрон помнит, кто он, между перезагрузками.
if [ -z "${AGENT_ID:-}" ] && [ -f "${BIND_FILE:-/data/binding.json}" ]; then
  AGENT_ID="$(python - <<'EOF' 2>/dev/null || true
import json, os
print(json.load(open(os.environ.get("BIND_FILE", "/data/binding.json"))).get("agent_id") or "")
EOF
)"
  [ -n "$AGENT_ID" ] && echo "[entrypoint] AGENT_ID из привязки: $AGENT_ID"
fi
AGENT_ID="${AGENT_ID:?set AGENT_ID (e.g. drone-5 / rover / painter-7) — или привяжите дрона через /fleet}"
export AGENT_ID
ROLE="${ROLE:?set ROLE (scout | rover | painter)}"
case "$ROLE" in
  painter) TASK="${TASK:-painting}" ;;
  *)       TASK="${TASK:-safe_passage}" ;;
esac
export TASK
export BRIDGE_URL="${BRIDGE_URL:-http://localhost:9000}"
if [ "$TASK" = "painting" ]; then export SCENARIO="${SCENARIO:-painters-1}";
else export SCENARIO="${SCENARIO:-scenario-1}"; fi

# --- pick / generate the SOUL ------------------------------------------------
BUNDLED="/app/souls/${AGENT_ID}.md"
if [ -f "$BUNDLED" ]; then
  export SOUL="$BUNDLED"
  echo "[entrypoint] $AGENT_ID using bundled soul $BUNDLED"
else
  # No bundled soul: write an IDENTITY-FREE soul. The painter's name/colour/
  # technique/temperament/leaning are generated per-run by persona(agent_id,
  # paint_seed) — so painter-7, painter-99, anything just works and scales with
  # no %4 wrap. Pin a field only if the operator sets the matching DRONE_* env.
  SOUL=/tmp/soul.md
  if [ "$ROLE" = "painter" ]; then
    {
      echo "---"
      echo "id: $AGENT_ID"
      [ -n "${DRONE_NAME:-}" ]      && echo "name: \"$DRONE_NAME\""
      echo "role: painter"
      echo "capabilities:"; echo "  - move"; echo "  - spray"
      echo "vote_weight: 1.0"
      [ -n "${DRONE_COLOR:-}" ]     && echo "color: \"$DRONE_COLOR\""
      [ -n "${DRONE_TECHNIQUE:-}" ] && echo "technique: $DRONE_TECHNIQUE"
      [ -n "${DRONE_TEMPERAMENT:-}" ] && echo "temperament: $DRONE_TEMPERAMENT"
      [ -n "${DRONE_LEANING:-}" ]   && echo "leaning: $DRONE_LEANING"
      echo "---"
      echo ""
      echo "Ты — дрон-художник. Пигмент, техника, характер и склонности выданы тебе"
      echo "на этот запуск (см. палитру и блок ХАРАКТЕР в задании). Предлагай кирпич"
      echo "своим цветом, спорь по имени в своём характере, голосуй за общий вариант."
      echo "Финал держим ПРОСТЫМ — несколько крупных фигур. Все сообщения — на русском."
    } > "$SOUL"
  else
    # NAME was never set anywhere — under `set -eu` expanding it killed the
    # container before launch (generated scout/rover souls were dead on arrival)
    NAME="${DRONE_NAME:-$AGENT_ID}"
    cat > "$SOUL" <<EOF
---
id: $AGENT_ID
name: $NAME
role: $ROLE
capabilities:
  - move
  - photograph
  - detect_obstacle
vote_weight: 1.0
priorities:
  - coverage_completeness
---

You are $NAME, a $ROLE drone. Favor thorough coverage with no blind spots.
Be terse. Only post a message if it adds information not already on the board.
EOF
  fi
  export SOUL
  echo "[entrypoint] $AGENT_ID generated identity-free soul ($ROLE; persona by seed${DRONE_COLOR:+, color $DRONE_COLOR})"
fi

# debug/test hook: print the resolved SOUL + key env, then exit (no brain launch)
if [ "${DUMP_SOUL:-0}" = "1" ]; then
  echo "[entrypoint] AGENT_ID=$AGENT_ID ROLE=$ROLE TASK=$TASK SCENARIO=$SCENARIO SOUL=$SOUL"
  echo "----- soul -----"; cat "$SOUL"; exit 0
fi

# --- launch the brain --------------------------------------------------------
# Default: the proven Python §7-loop wrapper (stdlib only, multi-arch, streams
# its thinking to the hub today). BRAIN=picoclaw is the native path: it needs
# the pinned PicoClaw binary baked in (build with --build-arg WITH_PICOCLAW=1)
# and a tiny sidecar piping PicoClaw's reasoning to the hub /events feed. We
# fall back to the wrapper if the binary is absent so the node still works.
if [ "${BRAIN:-wrapper}" = "picoclaw" ] && command -v picoclaw >/dev/null 2>&1; then
  echo "[entrypoint] launching native PicoClaw brain for $AGENT_ID"
  mkdir -p "$HOME/.picoclaw"
  # render the config skeleton, pointing the bridge MCP shim at this drone
  sed "s#http://localhost:9000#$BRIDGE_URL#g; s#/app/SOUL.md#$SOUL#g" \
      /app/drone/picoclaw.config.json > "$HOME/.picoclaw/config.json"
  # PicoClaw drives the agent; the bridge MCP shim gives it the robot tools.
  exec picoclaw run --config "$HOME/.picoclaw/config.json"
else
  [ "${BRAIN:-wrapper}" = "picoclaw" ] && \
    echo "[entrypoint] PicoClaw binary not baked in; falling back to wrapper brain"
  exec python /app/agent/loop.py
fi
