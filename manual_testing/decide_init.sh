#!/usr/bin/env bash
# Anchor a fake Claude session at the current tmux pane.
# Run this from the pane where you'd want notifications to be silent.
set -euo pipefail

SESSION_ID="${1:-manual-test}"
SESSION_DIR="/tmp/cc_notifier"
STATE_FILE="${SESSION_DIR}/${SESSION_ID}"

mkdir -p "$SESSION_DIR"

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux not found" >&2
  exit 1
fi

TS=$(tmux display-message -p '#{session_id}' 2>/dev/null || echo "")
TW=$(tmux display-message -p '#{window_id}' 2>/dev/null || echo "")
TP=$(tmux display-message -p '#{pane_id}' 2>/dev/null || echo "")

if [[ -z "$TS" ]]; then
  echo "Not inside tmux. Run this from inside a tmux pane." >&2
  exit 1
fi

# Try to capture focused window via Hammerspoon — okay if it fails
HAMMERSPOON_CLI="/Applications/Hammerspoon.app/Contents/Frameworks/hs/hs"
WINDOW_ID="UNAVAILABLE"
APP_PATH="UNAVAILABLE"
if [[ -x "$HAMMERSPOON_CLI" ]]; then
  HS_OUT=$("$HAMMERSPOON_CLI" -c "local w=hs.window.focusedWindow(); if w then local app=w:application(); print(w:id()..'|'..(app and app:path() or 'UNKNOWN')) else print('ERROR') end" 2>/dev/null || echo "ERROR")
  if [[ "$HS_OUT" != "ERROR" && "$HS_OUT" == *"|"* ]]; then
    WINDOW_ID="${HS_OUT%%|*}"
    APP_PATH="${HS_OUT#*|}"
  fi
fi

cat > "$STATE_FILE" <<EOF
{"version":2,"window_id":"$WINDOW_ID","app_path":"$APP_PATH","timestamp":0,"tmux_session_id":"$TS","tmux_window_id":"$TW","tmux_pane_id":"$TP"}
EOF

cat <<EOF
Anchored session "$SESSION_ID" at:
  tmux_session_id = $TS
  tmux_window_id  = $TW
  tmux_pane_id    = $TP
  window_id       = $WINDOW_ID
  app_path        = $APP_PATH

State written to: $STATE_FILE

Now move to whichever pane/window/app you want to test from, then run:
  decide_notify.sh $SESSION_ID
EOF
