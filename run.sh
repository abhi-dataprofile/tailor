#!/usr/bin/env bash
# run.sh — start the full Tailor stack: web server + (optional) background crawler + applier.
# Reads keys from ./.env  (copy .env.example → .env and fill it in).
#
#   ./run.sh              # server only  (local mode, or cloud if .env has Supabase)
#   ./run.sh --workers    # server + 6h crawler loop + background auto-applier
#
# The background workers only do anything when Supabase is configured in .env.
set -e
cd "$(dirname "$0")"

PY=python3
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"

# quick check: is Supabase configured?
CLOUD=$($PY -c "import supabase_client as s; print('yes' if s.is_configured() else 'no')" 2>/dev/null || echo no)
echo "Cloud (Supabase): $CLOUD"

pids=()
cleanup(){ echo; echo "stopping…"; for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup INT TERM EXIT

echo "→ web server on http://localhost:8765"
$PY serve.py &
pids+=($!)

if [ "$1" = "--workers" ]; then
  if [ "$CLOUD" = "yes" ]; then
    echo "→ crawler loop (refresh career pages every JOB_REFRESH_HOURS)"
    $PY worker.py &                       # loops by default
    pids+=($!)
    echo "→ background auto-applier every 10 min (only users who opted in)"
    ( while true; do $PY apply.py; sleep "${APPLY_LOOP_SLEEP:-600}"; done ) &
    pids+=($!)
  else
    echo "!! --workers ignored: Supabase not configured. Fill .env (see .env.example)."
  fi
fi

wait
