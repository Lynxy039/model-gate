#!/bin/sh
# экшен: открыть pane mgpan (вызывается из меню действий herdr)
exec "${HERDR_BIN_PATH:-herdr}" plugin pane open \
    --plugin lynxy.model-gate --entrypoint mgpan
