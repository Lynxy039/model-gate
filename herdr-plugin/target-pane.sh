#!/bin/sh
# общ: напечатать id целевого pane для сплита — фокусный (или первый) в workspace
HERDR="${HERDR_BIN_PATH:-herdr}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
python3.11 -c '
import json, subprocess, sys
out = subprocess.run(["'"$HERDR"'", "pane", "list", "--workspace", sys.argv[1]],
                     capture_output=True, text=True).stdout
panes = [p for p in json.loads(out)["result"]["panes"] if p.get("label") != "model-gate"]
target = next((p for p in panes if p.get("focused")), None) or next(iter(panes), None)
if target:
    print(target["pane_id"])
' "$1" 2>/dev/null
