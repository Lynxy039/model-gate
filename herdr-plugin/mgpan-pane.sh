#!/bin/sh
# mgpan в pane herdr: mgrun поднимет gate (если нужно) и запустит панель.
# PATH прописываем явно — окружение pane наследуется от herdr-сервера.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
exec /opt/homebrew/bin/python3.11 /Users/anton/Projects/model-gate/mgrun
