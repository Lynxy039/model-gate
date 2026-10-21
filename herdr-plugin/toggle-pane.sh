#!/bin/sh
# экшен toggle: закрыть панель в текущем workspace или открыть, если закрыта
HERDR="${HERDR_BIN_PATH:-herdr}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

ws=${HERDR_WORKSPACE_ID:-$(python3.11 -c '
import json, os
try:
    ctx = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
except ValueError:
    raise SystemExit(0)
for value in (ctx, ctx.get("workspace")):
    if isinstance(value, dict) and value.get("workspace_id"):
        print(value["workspace_id"])
        break
' 2>/dev/null)}
[ -n "$ws" ] || exit 1

pane=$(python3.11 -c '
import json, subprocess, sys
herdr = "'"$HERDR"'"
out = subprocess.run([herdr, "pane", "list", "--workspace", sys.argv[1]],
                     capture_output=True, text=True).stdout
for pane in json.loads(out)["result"]["panes"]:
    if pane.get("label") == "model-gate":
        print(pane["pane_id"])
        break
' "$ws" 2>/dev/null)

if [ -n "$pane" ]; then
    "$HERDR" plugin pane close "$pane" >/dev/null 2>&1
else
    target=$("$(dirname "$0")"/target-pane.sh "$ws")
    [ -n "$target" ] && "$HERDR" plugin pane open --plugin lynxy.model-gate \
        --entrypoint mgpan --target-pane "$target" \
        --direction right >/dev/null 2>&1
fi
exit 0
