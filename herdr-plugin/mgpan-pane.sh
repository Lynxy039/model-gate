#!/bin/sh
# mgpan в sidebar-режиме: mgrun поднимет gate (если нужно) и запустит панель.
# --no-gpu: панель висит в каждом workspace (×N процессов), а powermetrics
# каждый секнунду — слишком дорого для N экземпляров.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
exec /opt/homebrew/bin/python3.11 /Users/anton/Projects/model-gate/mgrun --no-gpu
