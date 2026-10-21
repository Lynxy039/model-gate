#!/bin/sh
# startup hook: повесить панель-сайдбар на каждый workspace, где её ещё нет
HERDR="${HERDR_BIN_PATH:-herdr}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

"$HERDR" workspace list | python3.11 -c '
import json, sys
for ws in json.load(sys.stdin)["result"]["workspaces"]:
    print(ws["workspace_id"])
' | while read -r ws; do
    has=$("$HERDR" pane list --workspace "$ws" | grep -c '"label": *"model-gate"')
    if [ "$has" = "0" ]; then
        target=$("$(dirname "$0")"/target-pane.sh "$ws")
        [ -n "$target" ] && "$HERDR" plugin pane open \
            --plugin lynxy.model-gate --entrypoint mgpan \
            --target-pane "$target" --direction right >/dev/null 2>&1
    fi
    sleep 1  # не поднимать все gate'ы одновременно через гонку в mgrun
done
exit 0
