"""Локальный терминальный монитор model-gate."""

from __future__ import annotations

import argparse
import curses
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def model_rows(status: dict) -> list[tuple[str, dict | None, bool, dict]]:
    return [
        (backend["name"], backend.get("process"), backend.get("ready", False), model)
        for backend in status.get("backends", [])
        for model in backend.get("models", [])
    ]


def _request(url: str, data: dict | None = None) -> dict:
    body = None if data is None else json.dumps(data).encode()
    request = Request(url, data=body, method="POST" if body else "GET")
    if body:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=2) as response:
            return json.loads(response.read())
    except HTTPError as error:
        try:
            message = json.loads(error.read()).get("error", str(error))
        except json.JSONDecodeError:
            message = str(error)
        raise RuntimeError(message) from error
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError(str(error)) from error


class ModelGateTui:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.selected = 0
        self.message = ""

    def run(self) -> None:
        curses.wrapper(self._run)

    def _run(self, screen) -> None:
        curses.curs_set(0)
        screen.timeout(1000)
        status: dict = {}
        rows: list[tuple[str, dict | None, bool, dict]] = []
        refresh = 0.0
        while True:
            if time.monotonic() >= refresh:
                try:
                    status = _request(f"{self.url}/admin/status")
                    rows = model_rows(status)
                    self.selected = min(self.selected, max(len(rows) - 1, 0))
                    self.message = ""
                except RuntimeError as error:
                    self.message = f"Ошибка: {error}"
                refresh = time.monotonic() + 1
            self._draw(screen, status, rows)
            key = screen.getch()
            if key in (ord("q"), 27):
                return
            if key in (curses.KEY_UP, ord("k")) and rows:
                self.selected = max(self.selected - 1, 0)
            elif key in (curses.KEY_DOWN, ord("j")) and rows:
                self.selected = min(self.selected + 1, len(rows) - 1)
            elif key == ord("s") and rows:
                self._stop_selected(screen, rows[self.selected])
                refresh = 0

    def _draw(self, screen, status: dict, rows: list[tuple[str, dict | None, dict]]) -> None:
        screen.erase()
        height, width = screen.getmaxyx()

        def line(y: int, text: str, attr: int = 0) -> None:
            if y < height:
                try:
                    screen.addnstr(y, 0, text, max(width - 1, 0), attr)
                except curses.error:
                    pass

        line(0, f"model-gate  {self.url}  |  q: выход  s: остановить", curses.A_BOLD)
        line(1, f"Активных запросов: {status.get('active', 0)}  |  Ожидают допуска: {status.get('waiting', 0)}")
        line(3, "backend     модель                              состояние")
        visible = max(height - 5, 0)
        first = min(max(self.selected - visible // 2, 0), max(len(rows) - visible, 0))
        for index, (backend, process, ready, model) in enumerate(rows[first:first + visible], first):
            flags = ["up" if ready else "down"]
            if model.get("running"):
                flags.append(f"RUN pid={process['pid']}")
            if model.get("loaded"):
                flags.append("loaded")
            if model.get("active"):
                flags.append(f"active={model['active']}")
            line(4 + index - first, f"{backend:<11} {model['id']:<35} {', '.join(flags)}", curses.A_REVERSE if index == self.selected else 0)
        footer = f"{self.selected + 1}/{len(rows)}  ↑↓/jk: выбор; s: остановить backend выбранной модели"
        line(height - 1, self.message or footer)
        screen.refresh()

    def _stop_selected(self, screen, row: tuple[str, dict | None, bool, dict]) -> None:
        backend, process, _, _ = row
        if not process:
            self.message = f"У {backend} нет процесса, запущенного model-gate"
            return
        height, _ = screen.getmaxyx()
        prompt = f"Остановить {backend}/{process.get('model') or 'backend'}? [y/N]"
        try:
            screen.addstr(height - 1, 0, prompt)
        except curses.error:
            return
        screen.refresh()
        if screen.getch() not in (ord("y"), ord("Y")):
            self.message = "Отменено"
            return
        try:
            _request(f"{self.url}/admin/stop", {"backend": backend})
            self.message = f"Остановлен {backend}"
        except RuntimeError as error:
            self.message = f"Ошибка: {error}"


def main() -> None:
    parser = argparse.ArgumentParser(description="TUI для локального model-gate")
    parser.add_argument("--url", default=os.environ.get("MODEL_GATE_URL", "http://127.0.0.1:9000"))
    args = parser.parse_args()
    ModelGateTui(args.url).run()


if __name__ == "__main__":
    main()
