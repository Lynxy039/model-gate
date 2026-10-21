#!/bin/sh
# event hook workspace.created: повесить панель на новый workspace
HERDR="${HERDR_BIN_PATH:-herdr}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
[ "${HERDR_PLUGIN_EVENT:-}" = "workspace.created" ] || exit 0

ws=$(python3.11 -c '
import json, os
try:
    event = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}")
except ValueError:
    raise SystemExit(0)
for value in (event, event.get("workspace"), event.get("payload"), event.get("data")):
    if isinstance(value, dict) and value.get("workspace_id"):
        print(value["workspace_id"])
        break
' 2>/dev/null)
if [ -n "$ws" ] || { ws=$(python3.11 -c '
import json, os
try:
    ctx = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
except ValueError:
    raise SystemExit(0)
for value in (ctx, ctx.get("workspace")):
    if isinstance(value, dict) and value.get("workspace_id"):
        print(value["workspace_id"])
        break
' 2>/dev/null); [ -n "$ws" ]; }; then
    target=$("$(dirname "$0")"/target-pane.sh "$ws")
    [ -n "$target" ] && "$HERDR" plugin pane open --plugin lynxy.model-gate \
        --entrypoint mgpan --target-pane "$target" \
        --direction right >/dev/null 2>&1
fi
exit 0
