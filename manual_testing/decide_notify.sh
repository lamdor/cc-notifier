#!/usr/bin/env bash
# Run cc-notifier notify against a previously anchored session.
# Reports which Decision (SILENT/LOCAL/PUSH) the new code chose.
set -euo pipefail

SESSION_ID="${1:-manual-test}"
SESSION_DIR="/tmp/cc_notifier"
STATE_FILE="${SESSION_DIR}/${SESSION_ID}"
LOG_FILE="$HOME/.cc-notifier/cc-notifier.log"

if [[ ! -f "$STATE_FILE" ]]; then
  echo "No anchored session at $STATE_FILE. Run decide_init.sh first." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON_BIN="$REPO_DIR/.venv/bin/python3"
SCRIPT="$REPO_DIR/cc_notifier.py"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "venv python not found at $PYTHON_BIN — run 'make install' in the repo first" >&2
  exit 1
fi

# Mark a known boundary in the log so we can show only this run's lines
BOUNDARY=">>> manual_test $(date '+%H:%M:%S.%N') <<<"
mkdir -p "$(dirname "$LOG_FILE")"
echo "$BOUNDARY" >> "$LOG_FILE"

# Reset timestamp so dedup doesn't suppress us
python3 -c "
import json
p = '$STATE_FILE'
d = json.load(open(p))
d['timestamp'] = 0
json.dump(d, open(p, 'w'))
"

PAYLOAD='{"session_id":"'"$SESSION_ID"'","cwd":"'"$PWD"'","hook_event_name":"Notification","message":"manual test"}'

CC_NOTIFIER_WRAPPER=1 \
  echo "$PAYLOAD" | CC_NOTIFIER_WRAPPER=1 "$PYTHON_BIN" "$SCRIPT" --debug notify || true

# Brief wait for the (synchronous, since we're not using the wrapper) call to complete
# Then print the decision line from the log
echo
echo "=== decision log ==="
awk -v b="$BOUNDARY" 'found {print} $0 == b {found=1}' "$LOG_FILE" | grep -E "decide:|Push check|Sending|notification" || echo "(no decide lines — check $LOG_FILE)"
