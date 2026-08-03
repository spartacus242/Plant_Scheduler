#!/usr/bin/env bash
# Start Flowstate (Streamlit) if it is not already running, then open it in Chrome.
# Safe to click repeatedly — idempotent.

set -euo pipefail

PORT="${FLOWSTATE_PORT:-8501}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
URL="http://localhost:${PORT}"
LOG="${FLOWSTATE_LOG:-/tmp/flowstate-streamlit.log}"
SESSION="flowstate-app"

is_up() {
  curl -sf -o /dev/null "http://127.0.0.1:${PORT}/_stcore/health" 2>/dev/null
}

start_app() {
  echo "Starting Flowstate on port ${PORT}..."
  cd "$REPO_ROOT"
  # Prefer tmux so the process survives the launcher exiting.
  if command -v tmux >/dev/null 2>&1; then
    TMUX_CONF="/exec-daemon/tmux.portal.conf"
    TMUX=(tmux)
    if [[ -f "$TMUX_CONF" ]]; then
      TMUX=(tmux -f "$TMUX_CONF")
    fi
    if ! "${TMUX[@]}" has-session -t "=$SESSION" 2>/dev/null; then
      "${TMUX[@]}" new-session -d -s "$SESSION" -c "$REPO_ROOT" -- \
        bash -lc "python3 -m streamlit run code/app.py --server.headless true --server.port ${PORT} 2>&1 | tee '${LOG}'"
    else
      # Session exists but health failed — restart the command inside it.
      "${TMUX[@]}" send-keys -t "$SESSION:0.0" C-c
      sleep 1
      "${TMUX[@]}" send-keys -t "$SESSION:0.0" \
        "cd '${REPO_ROOT}' && python3 -m streamlit run code/app.py --server.headless true --server.port ${PORT} 2>&1 | tee '${LOG}'" C-m
    fi
  else
    nohup python3 -m streamlit run code/app.py --server.headless true --server.port "$PORT" \
      >"$LOG" 2>&1 &
  fi

  # Wait up to ~30s for health.
  for _ in $(seq 1 60); do
    if is_up; then
      echo "Flowstate is ready."
      return 0
    fi
    sleep 0.5
  done
  echo "Timed out waiting for Flowstate at ${URL}." >&2
  echo "Check log: ${LOG}" >&2
  return 1
}

if ! is_up; then
  start_app
else
  echo "Flowstate already running at ${URL}"
fi

# Open in Chrome (Cloud Agent Desktop) or fall back to xdg-open.
if command -v google-chrome >/dev/null 2>&1; then
  google-chrome --new-window "$URL" >/dev/null 2>&1 &
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1 &
else
  echo "Open this URL in a browser: ${URL}"
fi
