"""mgpan — цветная TUI-панель мониторинга model-gate (curses, без зависимостей).

Gruvbox, логотип lynxy-dev, боксы: header+LIVE, SYSTEM (подбоксы RAM|GPU|CPU
с многоуровневыми графиками), BACKENDS (модели и метрики провайдеров),
МОДЕЛИ, RECENT. Источники: gate /admin/stats + /admin/status,
omlx /admin/api/stats, vm_stat/top/sysctl, опционально sudo powermetrics.
"""

from __future__ import annotations

import argparse
import curses
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

EIGHTHS = " ▁▂▃▄▅▆▇█"
HISTORY = 150
CHART_ROWS = 4

GRUVBOX_256 = {
    "ok": 142, "err": 167, "warn": 214, "head": 108, "live": 175,
    "dim": 245, "blue": 109, "orange": 208, "fg": 223,
}
GRUVBOX_16 = {
    "ok": curses.COLOR_GREEN, "err": curses.COLOR_RED, "warn": curses.COLOR_YELLOW,
    "head": curses.COLOR_CYAN, "live": curses.COLOR_MAGENTA, "dim": curses.COLOR_WHITE,
    "blue": curses.COLOR_BLUE, "orange": curses.COLOR_YELLOW, "fg": curses.COLOR_WHITE,
}
BOLD_ROLES = ("head", "live")

LOGO = [
    ("██╗     ", "ok", "██╗  ██╗", "blue"),
    ("██║     ", "ok", "╚██╗██╔╝", "blue"),
    ("██║      ", "blue", "╚███╔╝", "warn"),
    ("██║      ", "blue", "██╔██╗", "warn"),
    ("███████╗", "warn", "██╔╝ ██╗", "ok"),
    ("╚══════╝", "dim", "╚═╝  ╚═╝", "ok"),
]
LOGO_WIDTH = 16

Seg = tuple[str, str]


def fetch_json(url: str, timeout: float = 2.0):
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def post_json(url: str, payload: dict, timeout: float = 15.0, headers: dict | None = None) -> dict:
    body = json.dumps(payload).encode()
    request = Request(url, data=body, method="POST",
                      headers={"Content-Type": "application/json", **(headers or {})})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


def build_targets(state: dict) -> list[dict]:
    """Плоский список управляемых объектов под курсор j/k."""
    targets: list[dict] = []
    for entry in (state.get("gate_stats") or {}).get("inflight", []):
        targets.append({"kind": "inflight", "id": entry["id"], "backend": entry["backend"],
                        "label": f"req #{entry['id']} {entry['model'][:24]} {entry['elapsed_s']:.0f}s", "keys": "x"})
    for backend in (state.get("gate_status") or {}).get("backends", []):
        managed = backend.get("process") is not None
        targets.append({"kind": "backend", "backend": backend["name"], "managed": managed,
                        "label": f"backend {backend['name']}", "keys": "s r" if managed else "o"})
        for model in backend.get("models", []):
            loaded = bool(model.get("loaded") or model.get("running") or model.get("active"))
            keys = "w" + (" u" if backend["name"] == "omlx" and loaded else "")
            targets.append({"kind": "model", "backend": backend["name"], "id": model["id"],
                            "label": f"model {model['id'][:30]} ({backend['name']})", "keys": keys})
    return targets


class Actions:
    """Управляющие команды: fire-and-forget поток + строка статуса в футер."""

    def __init__(self, gate_url: str, omlx_url: str):
        self.gate_url = gate_url.rstrip("/")
        self.omlx_url = omlx_url.rstrip("/")
        self.lock = threading.Lock()
        self._status, self._expires = "", 0.0

    def _set(self, text: str) -> None:
        with self.lock:
            self._status, self._expires = text, time.monotonic() + 8

    def status(self) -> str:
        with self.lock:
            return self._status if time.monotonic() < self._expires else ""

    def _run(self, label: str, call) -> None:
        def worker():
            try:
                call()
                self._set(f"{label}: ok")
            except Exception as error:  # noqa: BLE001 — что угодно с апстрима пишем в футер, не в краш
                self._set(f"{label}: {str(error)[:70]}")
        self._set(f"{label}: sending…")
        threading.Thread(target=worker, daemon=True).start()

    def stop(self, backend: str) -> None:
        self._run(f"stop {backend}", lambda: post_json(f"{self.gate_url}/admin/stop", {"backend": backend}))

    def warm(self, model: str) -> None:
        payload = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "warmup"}]}
        self._run(f"warmup {model[:24]}",
                  lambda: post_json(f"{self.gate_url}/v1/chat/completions", payload, timeout=600))

    def restart(self, backend: str, model: str | None) -> None:
        self.stop(backend)
        if model:
            self.warm(model)

    def cancel(self, request_id: int) -> None:
        self._run(f"cancel #{request_id}", lambda: post_json(f"{self.gate_url}/admin/cancel", {"id": request_id}))

    def omlx_unload(self, model_id: str, api_key: str) -> None:
        url = f"{self.omlx_url}/admin/api/models/{model_id}/unload"
        self._run(f"unload {model_id[:24]}",
                  lambda: post_json(url, {}, headers={"Authorization": f"Bearer {api_key}"}))

    def omlx_restart(self, cli: str) -> None:
        self._run("omlx restart",
                  lambda: subprocess.run(shlex.split(cli) + ["restart"], capture_output=True, text=True, timeout=180))

    def stop_gate(self, pid_file: str = "/tmp/model-gate.pid") -> None:
        def call():
            pid = int(Path(pid_file).read_text().strip())
            os.kill(pid, signal.SIGTERM)
        self._run("stop gate", call)


def fmt_n(value: float | int | None) -> str:
    if value is None:
        return "—"
    value = float(value)
    for suffix, limit in (("M", 1_000_000), ("k", 1_000)):
        if value >= limit:
            return f"{value / limit:.1f}{suffix}"
    return str(int(value)) if value == int(value) else f"{value:.1f}"


def fmt_g(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s"


def gb(bytes_value: float | None) -> str:
    return "—" if bytes_value is None else f"{bytes_value / 1024 ** 3:.1f}G"


def vchart(values, width: int, rows: int = CHART_ROWS, ceiling: float = 1.0) -> list[str]:
    """Многоуровневый столбчатый график; последние значения справа."""
    vals = list(values)[-width:]
    vals = [None] * (width - len(vals)) + vals
    columns = []
    for value in vals:
        if value is None:
            columns.append([" "] * rows)
            continue
        level = max(0.0, min(float(value) / ceiling, 1.0)) * rows
        if float(value) > 0:
            level = max(level, 0.25)  # ненулевое значение всегда видно (минимум ▂)
        columns.append([
            EIGHTHS[max(0, min(8, round((level - (rows - 1 - r)) * 8)))] for r in range(rows)
        ])
    return ["".join(column[r] for column in columns) for r in range(rows)]


def load_role(fraction) -> str:
    if fraction is None:
        return "dim"
    return "ok" if fraction < 0.7 else ("warn" if fraction < 0.9 else "err")


# ---------------------------------------------------------------- macOS metrics

def read_vm_stat() -> dict:
    raw = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    page = int(re.search(r"page size of (\d+)", raw).group(1))
    pages = {m.group(1).lower(): int(m.group(2))
             for m in re.finditer(r"Pages (\w+(?: \w+)*):\s+(\d+)\.", raw)}
    total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout)
    free = pages.get("free", 0) * page
    wired = pages.get("wired down", 0) * page
    compressed = pages.get("occupied by compressor", 0) * page
    used = total - free
    return {
        "mem_total": total, "mem_used": used, "mem_wired": wired, "mem_compressed": compressed,
        "mem_frac": used / total if total else None,
        "swap": read_swap(),
    }


def read_swap() -> tuple[int, int]:
    raw = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout
    match = re.findall(r"(total|used|free) = ([\d.]+)([MK])", raw)
    values = {k: float(v) * (1024 ** 2 if unit == "M" else 1024 ** 3) for k, v, unit in match}
    return int(values.get("used", 0)), int(values.get("total", 0))


def read_cpu_busy() -> float | None:
    try:
        raw = subprocess.run(["top", "-l", "2", "-n", "0", "-s", "1"], capture_output=True, text=True, timeout=6).stdout
        samples = re.findall(r"CPU usage:\s*[\d.]+% user,\s*[\d.]+% sys,\s*([\d.]+)% idle", raw)
        return 100.0 - float(samples[-1]) if samples else None
    except (subprocess.TimeoutExpired, ValueError, IndexError):
        return None


def read_process_rss(patterns: dict[str, str]) -> dict[str, int]:
    raw = subprocess.run(["ps", "-axo", "rss=,comm="], capture_output=True, text=True).stdout
    result = {name: 0 for name in patterns}
    for line in raw.splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) != 2:
            continue
        rss, comm = parts
        for name, needle in patterns.items():
            if needle.lower() in comm.lower():
                result[name] += int(rss) * 1024
    return result


# ---------------------------------------------------------------- poller

class Poller(threading.Thread):
    def __init__(self, gate_url: str, omlx_url: str, gpu: bool):
        super().__init__(daemon=True)
        self.gate_url = gate_url.rstrip("/")
        self.omlx_url = omlx_url.rstrip("/")
        self.gpu_enabled = gpu
        self.lock = threading.Lock()
        self.state: dict = {
            "gate_stats": None, "gate_status": None, "omlx": None,
            "gate_error": "", "omlx_error": "",
            "sys": {}, "rss": {}, "cpu": None,
            "gpu_pct": None, "gpu_watts": None, "sys_watts": None, "gpu_note": "off (--no-gpu)" if not gpu else "",
            "hist": {k: deque(maxlen=HISTORY) for k in ("cpu", "mem", "pool", "gpu", "watts")},
            "rates": {"req_min": 0},
        }
        self._gpu_proc: subprocess.Popen | None = None
        self._gpu_log = None
        self._stop = threading.Event()

    def _start_gpu(self) -> None:
        try:
            self._gpu_log = open("/tmp/mgpan-gpu.log", "w")
            self._gpu_proc = subprocess.Popen(
                # без -n: на macOS 26 "-n 0" больше не «бесконечно», а «ноль сэмплов»
                ["sudo", "-n", "powermetrics", "--samplers", "gpu_power,cpu_power", "-i", "1000"],
                stdout=subprocess.PIPE, stderr=self._gpu_log, text=True,
            )
        except OSError as error:
            self.state["gpu_note"] = f"failed: {error}"

    def _gpu_error(self) -> str:
        try:
            tail = open("/tmp/mgpan-gpu.log").read().strip().splitlines()[-1:]
        except OSError:
            tail = []
        return tail[0][:60] if tail else ""

    def _pump_gpu(self) -> None:
        dbg = open("/tmp/mgpan-gpu-dbg.log", "w", buffering=1)
        dbg.write(f"pump started {time.strftime('%H:%M:%S')}\n")
        try:
            self._pump_gpu_inner(dbg)
        except Exception:
            import traceback
            dbg.write("CRASH:\n" + traceback.format_exc())
            with self.lock:
                self.state["gpu_note"] = "pump crashed (see /tmp/mgpan-gpu-dbg.log)"
        dbg.write("pump ended\n")

    def _pump_gpu_inner(self, dbg) -> None:
        if not self._gpu_proc:
            dbg.write("no gpu proc\n")
            return
        buffer = ""
        lines = 0
        while not self._stop.is_set():
            if self._gpu_proc.poll() is not None:
                dbg.write(f"proc exit code={self._gpu_proc.returncode}\n")
                with self.lock:
                    self.state["gpu_note"] = f"powermetrics exit: {self._gpu_error() or 'code ' + str(self._gpu_proc.returncode)}"
                return
            line = self._gpu_proc.stdout.readline()
            if not line:
                code = self._gpu_proc.poll()
                dbg.write(f"EOF on stdout, poll={code}\n")
                with self.lock:
                    self.state["gpu_note"] = (f"powermetrics exit: {self._gpu_error() or 'code ' + str(code)}"
                                              if code is not None else "powermetrics: no data")
                return
            lines += 1
            if lines <= 60 or (line.strip() and lines <= 3000):
                dbg.write(f"raw[{lines}]: {line.rstrip()}\n")
            buffer += line
            if line.strip():
                continue  # копит секцию до пустой строки: Power идёт после residency
            busy_pct, watts = self._parse_gpu_sample(buffer)
            sys_watts = self._parse_system_watts(buffer)
            buffer = ""
            if sys_watts is not None:
                with self.lock:
                    if self.state.get("sys_watts") != sys_watts:
                        self.state["hist"]["watts"].append(sys_watts)
                    self.state["sys_watts"] = sys_watts
            if busy_pct is None:
                continue
            with self.lock:
                self.state["gpu_pct"] = busy_pct
                self.state["gpu_watts"] = watts
                self.state["hist"]["gpu"].append(busy_pct)
                self.state["gpu_note"] = ""

    # sampler "cpu_power" на Apple Silicon: Combined Power (CPU + GPU + ANE) — оценка пакета SoC
    SYSTEM_WATTS_RE = re.compile(
        r"(?:System on Board Power|Combined Power[^:]*|Package Power):\s*([\d.]+)\s*(m?W)")

    @classmethod
    def _parse_system_watts(cls, section: str) -> float | None:
        match = cls.SYSTEM_WATTS_RE.search(section)
        if not match:
            return None
        return float(match.group(1)) * (0.001 if match.group(2) == "mW" else 1.0)

    @staticmethod
    def _parse_gpu_sample(section: str) -> tuple[float | None, float | None]:
        act = re.search(r"GPU Active Time:\s*([\d.]+)\s*ms", section)
        idl = re.search(r"GPU Idle Time:\s*([\d.]+)\s*ms", section)
        busy_pct = None
        if act and idl:
            busy = float(act.group(1))
            busy_pct = 100.0 * busy / (busy + float(idl.group(1)))
        else:
            # macOS 26 / Apple Silicon: "GPU idle residency: N%"
            resid = re.search(r"GPU idle residency:\s*([\d.]+)\s*%", section)
            # Intel: "Idle Factor"; старые macOS: "Idle Ratio"; "GPU Utilization/Busy: N%"
            direct = re.search(r"GPU (?:Utilization|Busy):\s*([\d.]+)\s*%", section)
            factor = re.search(r"Idle (?:Factor|Ratio):\s*([\d.]+)", section)
            if resid:
                busy_pct = 100.0 - min(100.0, float(resid.group(1)))
            elif direct:
                busy_pct = min(100.0, float(direct.group(1)))
            elif factor:
                busy_pct = 100.0 * (1.0 - min(1.0, float(factor.group(1))))
        if busy_pct is None:
            return None, None
        watts = None
        power = re.search(r"GPU Power:\s*([\d.]+)\s*(m?W)", section)
        if power:
            watts = float(power.group(1)) * (0.001 if power.group(2) == "mW" else 1.0)
        return busy_pct, watts

    def run(self) -> None:
        if self.gpu_enabled:
            self._start_gpu()
            threading.Thread(target=self._pump_gpu, daemon=True).start()
        cycle = 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                stats = fetch_json(f"{self.gate_url}/admin/stats")
                status = fetch_json(f"{self.gate_url}/admin/status")
                with self.lock:
                    self.state["gate_stats"], self.state["gate_status"], self.state["gate_error"] = stats, status, ""
            except (URLError, OSError, ValueError) as error:
                with self.lock:
                    self.state["gate_error"] = str(error)
            if cycle % 2 == 0:
                try:
                    omlx = fetch_json(f"{self.omlx_url}/admin/api/stats")
                    with self.lock:
                        self.state["omlx"], self.state["omlx_error"] = omlx, ""
                except (URLError, OSError, ValueError):
                    with self.lock:
                        self.state["omlx_error"] = "unavailable"
                try:
                    with self.lock:
                        self.state["sys"] = read_vm_stat()
                        self.state["rss"] = read_process_rss({"ds4": "ds4-server", "omlx": "omlx"})
                        self.state["hist"]["mem"].append(self.state["sys"].get("mem_frac"))
                    pool = (self.state.get("omlx") or {}).get("active_models", {})
                    if pool.get("model_memory_max"):
                        self.state["hist"]["pool"].append(pool["model_memory_used"] / pool["model_memory_max"])
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
            cpu = read_cpu_busy()
            with self.lock:
                self.state["cpu"] = cpu
                self.state["hist"]["cpu"].append(cpu)
            self._update_rates()
            self._stop.wait(max(0.2, 1.1 - (time.monotonic() - started)))
            cycle += 1

    def _update_rates(self) -> None:
        stats = self.state.get("gate_stats") or {}
        recent = stats.get("recent", [])
        cutoff = time.time() - 60
        with self.lock:
            self.state["rates"]["req_min"] = sum(1 for r in recent if r["ts"] >= cutoff)

    def stop(self) -> None:
        self._stop.set()
        if self._gpu_proc:
            self._gpu_proc.terminate()


# ---------------------------------------------------------------- box helpers

def box(title: str, inner: list[list[Seg]], width: int) -> list[list[Seg]]:
    inner_width = width - 4
    lines: list[list[Seg]] = [[("┌─ ", "dim"), (title, "head"),
                               (" " + "─" * max(0, width - len(title) - 5), "dim"), ("┐", "dim")]]
    for segs in inner:
        used = sum(len(text) for text, _ in segs)
        pad = max(0, inner_width - used)
        lines.append([("│ ", "dim")] + segs + [(" " * pad, "dim"), (" │", "dim")])
    lines.append([("└" + "─" * (width - 2) + "┘", "dim")])
    return lines


def merge_boxes(boxes: list[list[list[Seg]]], gap: int = 1) -> list[list[Seg]]:
    """Подбоксы рядом: все строки box() выровнены по своей ширине."""
    widths = [max(sum(len(t) for t, _ in line) for line in b) for b in boxes]
    height = max(len(b) for b in boxes)
    out: list[list[Seg]] = []
    for row in range(height):
        segs: list[Seg] = []
        for index, sub in enumerate(boxes):
            if row < len(sub):
                segs += sub[row]
            else:
                segs += [(" " * widths[index], "")]
            if index < len(boxes) - 1:
                segs += [(" " * gap, "")]
        out.append(segs)
    return out


# ---------------------------------------------------------------- sections

def header_section(state: dict, width: int, sel: dict | None = None) -> list[list[Seg]]:
    """Логотип слева; справа — бокс статуса gate и под ним бокс LIVE."""
    stats = state.get("gate_stats") or {}
    status = state.get("gate_status") or {}
    metrics = stats.get("backend_metrics", {})
    inflight = stats.get("inflight", [])
    live_tps = sum(
        entry["approx_completion_tokens"] / (entry["elapsed_s"] - (entry["ttft_s"] or 0))
        for entry in inflight
        if entry.get("approx_completion_tokens") and entry["elapsed_s"] - (entry["ttft_s"] or 0) > 0.3
    )
    prefill = max((m.get("prefill_avg_tps") or m.get("prefill_last_tps") or 0 for m in metrics.values()), default=0)

    right_width = width - LOGO_WIDTH - 5  # рамка лого (18+4) + зазор, впритык к краям
    if state.get("gate_error"):
        info: list[list[Seg]] = [
            [("model-gate is NOT RESPONDING", "err")],
            [(str(state["gate_error"])[:right_width - 4], "dim")],
        ]
    else:
        active = status.get("active", 0)
        queue = status.get("waiting_exclusive", 0)
        # все показатели одной строкой: подписи тусклые, значения цветные
        info = [[
            ("up ", "dim"), (fmt_g(stats.get("uptime_s")), "fg"),
            (" · ", "dim"), ("active ", "dim"), (str(active), "live" if active else "dim"),
            (" · ", "dim"), ("queue ", "dim"), (str(queue), "warn" if queue else "dim"),
            (" · ", "dim"), (f"{state['rates']['req_min']} req/min", "blue"),
            (" · ", "dim"), ("gen ", "dim"), (f"{live_tps:.1f} t/s", "orange"),
            (" · ", "dim"), ("pre ", "dim"), (f"{prefill:.0f} t/s", "orange"),
            (" · ", "dim"), (f"{datetime.now():%H:%M:%S}", "dim"),
        ]]
    live_inner: list[list[Seg]] = []
    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time()) % 10]
    if not inflight:
        live_inner = [[(f"{spinner} ", "dim"), ("idle", "dim")]]
    for entry in inflight[:3]:
        elapsed, ttft = entry["elapsed_s"], entry["ttft_s"]
        approx = entry.get("approx_completion_tokens") or 0
        gen_time = (elapsed - ttft) if ttft else 0
        tps = (approx / gen_time) if approx and gen_time > 0.3 else None
        cursor = sel and sel["kind"] == "inflight" and sel["id"] == entry["id"]
        # спиннер крутится, фаза подсвечена: prefill оранжевый (GPU пашет), генерация розовая
        segs: list[Seg] = [("▸" if cursor else " ", "live"), (f" {spinner} ", "live"),
                           (f"{entry['model'][:24]}", "fg"), ("  ", "")]
        if ttft:
            segs.append(("gen", "live"))
        else:
            segs.append(("prefill", "orange"))
        segs += [(f" {elapsed:.1f}s", "dim"), ("  ", "")]
        if ttft:
            segs.append((f"{approx} tok", "fg"))
        else:
            segs.append((f"{entry.get('context_chars', 0) // 4 // 1000}k ctx", "blue"))
        if tps:
            segs.append((f"  {tps:.1f} t/s", "orange"))
        live_inner.append(segs)
    if len(inflight) > 3:
        live_inner.append([("      ", ""), (f"+{len(inflight) - 3} more", "dim")])

    right = box("model-gate", info, right_width)

    # лого с подписью в своей рамке, высота выровнена по стопке правых боксов
    logo_inner: list[list[Seg]] = []
    for left, left_role, mid, mid_role in LOGO:
        logo_inner.append([(left, left_role), (mid, mid_role),
                           (" " * (LOGO_WIDTH - len(left) - len(mid)), "")])
    caption = "-lynxy-dev-"
    pad = max(0, (LOGO_WIDTH - len(caption)) // 2)
    logo_inner.append([(" " * pad, ""), (caption, "head"),
                       (" " * (LOGO_WIDTH - pad - len(caption)), "")])
    live_box = box("LIVE", live_inner, right_width)

    def spark(values, ceiling: float, n: int) -> str:
        tail = list(values)[-n:] or [0.0]
        return "".join(EIGHTHS[max(0, min(8, round(v / max(ceiling, 1e-9) * 8)))] for v in tail)

    # бокс общего энергопотребления системы (sampler "power" в том же powermetrics)
    sys_watts = state.get("sys_watts")
    watts_hist = list((state.get("hist") or {}).get("watts") or [])
    power_box: list[list[Seg]] = []
    if sys_watts is not None or watts_hist:
        peak = max(watts_hist[-600:], default=None)
        ceiling = max(peak or 1.0, sys_watts or 0.0, 1.0)
        power_rows = [[("⚡ ", "warn"),
                       (f"{sys_watts:.1f} W" if sys_watts is not None else "—", "warn"),
                       ("  peak ", "dim"), (f"{peak:.0f} W" if peak else "—", "dim"),
                       ("  gpu ", "dim"),
                       (f"{state.get('gpu_watts') or 0:.0f} W", "orange"),
                       ("  " + spark(watts_hist, ceiling, right_width - 30), "warn")]]
        power_box = box("POWER", power_rows, right_width)

    # боксы правого столбца стоят вплотную друг к другу; если стек ниже лого-рамки — добор снизу
    stack = power_box + live_box
    height = max(3 + len(stack), len(logo_inner) + 2)
    right += stack + [[("", "")]] * max(0, height - 3 - len(stack))
    logo_inner += [[]] * (height - 2 - len(logo_inner))
    logo_box: list[list[Seg]] = [[("┌" + "─" * (LOGO_WIDTH + 2) + "┐", "dim")]]
    for segs in logo_inner:
        used = sum(len(text) for text, _ in segs)
        logo_box.append([("│ ", "dim")] + segs + [(" " * (LOGO_WIDTH - used), "dim"), (" │", "dim")])
    logo_box.append([("└" + "─" * (LOGO_WIDTH + 2) + "┘", "dim")])

    lines: list[list[Seg]] = []
    for row in range(height):
        lines.append(logo_box[row] + [(" ", "")] + right[row])
    return lines


def system_section(state: dict, width: int) -> list[list[Seg]]:
    sysinfo = state.get("sys") or {}
    hist = state["hist"]
    cpu = state.get("cpu")
    gpu_pct = state.get("gpu_pct")
    rss = state.get("rss") or {}
    pool = ((state.get("omlx") or {}).get("active_models") or {})
    pool_frac = pool.get("model_memory_used", 0) / pool["model_memory_max"] if pool.get("model_memory_max") else None

    inner_width = width - 4
    sub_width = (inner_width - 2) // 3
    chart_width = sub_width - 4

    mem_frac = sysinfo.get("mem_frac")
    ram_head = f"{gb(sysinfo.get('mem_used'))}/{gb(sysinfo.get('mem_total'))}"
    cpu_head = f"{cpu:.0f}%" if cpu is not None else "—"
    if gpu_pct is not None:
        # с --gpu главный график — честная утилизация, pool уходит в подпись
        gpu_head = f"{gpu_pct:.0f}%" + (f"  pool {gb(pool.get('model_memory_used'))}" if pool else "")
        gpu_values, gpu_ceiling, gpu_fraction = hist["gpu"], 100.0, gpu_pct / 100
    elif state.get("gpu_note"):
        gpu_head = state["gpu_note"]
        gpu_values, gpu_ceiling, gpu_fraction = (hist["pool"] if pool else hist["gpu"]), 1.0, pool_frac
    else:
        gpu_head = f"pool {gb(pool.get('model_memory_used'))}/{gb(pool.get('model_memory_max'))}" if pool else (state.get("gpu_note") or "—")
        gpu_values, gpu_ceiling, gpu_fraction = (hist["pool"] if pool else hist["gpu"]), 1.0, pool_frac

    def sub(title: str, head: str, values, ceiling: float, fraction) -> list[list[Seg]]:
        # шапку режем по ширине подбокса: иначе длинная ошибка залезает на соседний бокс
        inner: list[list[Seg]] = [[(head[:sub_width - 4], load_role(fraction))]]
        for line in vchart(values, chart_width, CHART_ROWS, ceiling):
            inner.append([(line, load_role(fraction))])
        return box(title, inner, sub_width)

    subs = merge_boxes([
        sub("RAM", ram_head, hist["mem"], 1.0, mem_frac),
        sub("GPU", gpu_head, gpu_values, gpu_ceiling, gpu_fraction),
        sub("CPU", cpu_head, hist["cpu"], 100.0, None if cpu is None else cpu / 100),
    ])
    swap_used, swap_total = sysinfo.get("swap", (0, 0))
    watts = state.get("gpu_watts")
    gpu_note = state.get("gpu_note", "—")
    if gpu_pct is not None and watts is not None:
        gpu_note = f"{gpu_pct:.0f} {watts:.1f}W"
    footer = (f"wired {gb(sysinfo.get('mem_wired'))} · compressor {gb(sysinfo.get('mem_compressed'))}"
              f" · swap {gb(swap_used)}/{gb(swap_total)}"
              f" · GPU% {gpu_note if gpu_pct is not None else state.get('gpu_note', '—')}"
              f" · ds4 {gb(rss.get('ds4'))} · omlx {gb(rss.get('omlx'))}")
    return box("SYSTEM", subs + [[(footer, "dim")]], width)


def backends_section(state: dict, width: int, sel: dict | None = None) -> list[list[Seg]]:
    """Каждый провайдер — отдельный бокс, рядом |ds4|omlx|, модели списком."""
    status = state.get("gate_status") or {}
    stats = state.get("gate_stats") or {}
    metrics = stats.get("backend_metrics", {})
    omlx = state.get("omlx") or {}
    sub_width = (width - 4 - 1) // 2
    now = time.time()
    groups: list[tuple[str, list[list[Seg]]]] = []
    for backend in sorted(status.get("backends", []), key=lambda b: b["name"]):
        name = backend["name"]
        up = backend.get("ready")
        process = backend.get("process") or {}
        cursor = sel and sel["kind"] == "backend" and sel["backend"] == name
        # имя провайдера уже в заголовке бокса — внутри только статус и pid
        head: list[Seg] = [(f"{'▸ ' if cursor else '  '}{'●' if up else '○'} {'up' if up else 'down'}", "ok" if up else "err")]
        if process.get("pid"):
            head.append((f"  pid {process['pid']}", "dim"))
        inner: list[list[Seg]] = [head]
        if name == "omlx" and omlx:
            inner.append([("  prefill {} · gen {} t/s".format(omlx.get("avg_prefill_tps", "?"), omlx.get("avg_generation_tps", "?")), "orange")])
            inner.append([("  cache {}% · up {}".format(omlx.get("cache_efficiency", "?"), fmt_g(omlx.get("uptime_seconds"))), "dim")])
        else:
            extra = metrics.get(name, {})
            bits = []
            if extra.get("prefill_avg_tps") or extra.get("prefill_last_tps"):
                peak = extra.get("prefill_peak_tps") or 0
                bits.append(f"prefill {extra.get('prefill_avg_tps') or extra.get('prefill_last_tps'):.0f} t/s"
                            + (f" (peak {peak:.0f})" if peak else ""))
            if extra.get("gen_last_tps"):
                bits.append(f"gen {extra['gen_last_tps']:.1f} t/s")
            if bits:
                inner.append([("  " + " · ".join(bits), "orange")])
            # кэш-хит и аптайм считаем по запросам гейта к этому бэкенду
            totals = [(m["prompt_tokens"], m.get("cached_tokens") or 0)
                      for key, m in stats.get("models", {}).items() if key.startswith(name + "/")]
            prompt = sum(p for p, _ in totals)
            cached = sum(c for _, c in totals)
            tail = []
            if prompt:
                tail.append(f"cache {100 * cached / prompt:.0f}%")
            if process.get("started_ts"):
                tail.append(f"up {fmt_g(now - process['started_ts'])}")
            if tail:
                inner.append([("  " + " · ".join(tail), "dim")])
        inner.append([("", "")])
        for model in backend.get("models", []):
            selected = sel and sel["kind"] == "model" and sel["backend"] == name and sel["id"] == model["id"]
            if model.get("active"):
                role, mark = "live", "▶"
            elif model.get("loaded") or model.get("running"):
                role, mark = "ok", "◆"
            else:
                role, mark = "dim", "·"
            if selected:
                role, mark = "live", "▸"
            inner.append([(f" {mark} {model['id'][:sub_width - 6]}", role)])
        groups.append((name, inner))
    if not groups:
        return box("BACKENDS", [[(" no data from gate", "warn")]], width)
    # высоты боксов синхронны: короткий дополняем пустыми строками до максимума
    height = max(len(inner) for _, inner in groups)
    boxes = [box(name, inner + [[]] * (height - len(inner)), sub_width)
             for name, inner in groups]
    return box("BACKENDS", merge_boxes(boxes, gap=1), width)


def models_section(state: dict, width: int) -> list[list[Seg]]:
    stats = state.get("gate_stats") or {}
    models = stats.get("models", {})
    inner: list[list[Seg]] = []
    if not models:
        inner.append([(" no traffic yet", "dim")])
    else:
        inner.append([(" model" + " " * 29, "dim"), (f"{'req':>5}", "dim"), (f"{'in':>7}", "dim"),
                      (f"{'out':>7}", "dim"), (f"{'cache':>6}", "dim"), (f"{'pre t/s':>8}", "dim"),
                      (f"{'gen t/s':>8}", "dim"), (f"{'last':>7}", "dim")])
        for key in sorted(models, key=lambda k: -models[k]["completion_tokens"]):
            model = models[key]
            cache = model.get("cache_hit_rate")
            avg_tps = model.get("avg_tps")
            last_elapsed = model.get("last_elapsed_s")
            prefill = model.get("last_prefill_tps")
            short = key.replace("ds4/", "d:").replace("omlx/", "o:")
            role = "ok" if model["errors"] == 0 else "warn"
            inner.append([(f" {short[:30]:<34}", role),
                          (f"{model['requests']:>5}", "fg"),
                          (f"{fmt_n(model['prompt_tokens']):>7}", "fg"),
                          (f"{fmt_n(model['completion_tokens']):>7}", "fg"),
                          (f"{(f'{cache:.0f}%' if cache is not None else '—'):>6}", "fg"),
                          (f"{(f'{prefill:.0f}' if prefill else '—'):>8}", "orange"),
                          (f"{(f'{avg_tps:.1f}' if avg_tps else '—'):>8}", "fg"),
                          (f"{(f'{last_elapsed:.1f}s' if last_elapsed else '—'):>7}", "dim")])
    return box("MODELS (since gate start)", inner, width)


def recent_section(state: dict, width: int, limit: int) -> list[list[Seg]]:
    stats = state.get("gate_stats") or {}
    recent = state.get("_recent_view", stats.get("recent", []))[:limit]
    inner: list[list[Seg]] = []
    if not recent:
        inner.append([(" no traffic yet", "dim")])
        return box("RECENT REQUESTS  (PgUp/PgDn)", inner, width)
    # колонки: каждая своим цветом — время тусклое, модель обычная, статус
    # по результату, токены синие, кэш розовый, скорости оранжевые
    inner.append([(f" {'time':<8}", "dim"), (f" {'model':<24}", "dim"), (" " + f"{'code':>4}", "dim"),
                  (" " + f"{'dur':>7}", "dim"), (f" {'in':>7}", "blue"), (f" {'out':>6}", "blue"),
                  (f" {'cache':>6}", "live"), (" " + f"{'gen t/s':>8}", "orange"), (f" {'pre t/s':>8}", "orange")])
    for record in recent:
        stamp = datetime.fromtimestamp(record["ts"]).strftime("%H:%M:%S")
        if record.get("disconnected"):
            flag, role = "ABRT", "warn"
        elif record["status"] >= 400:
            flag, role = str(record["status"]), "err"
        else:
            flag, role = "200", "ok"
        tps = f"{record['tps']:.1f}" if record.get("tps") else "—"
        prefill = f"{record['prefill_tps']:.0f}" if record.get("prefill_tps") else "—"
        prompt, cached = record.get("prompt_tokens") or 0, record.get("cached_tokens") or 0
        reuse = f"{100 * cached / prompt:.0f}%" if prompt and cached else "—"
        inner.append([(" " + f"{stamp:<8}", "dim"),
                      (f" {record['model'][:24]:<24}", "fg"),
                      (f"{flag:>5}", role),
                      (f" {record['elapsed_s']:>6.2f}s", "fg"),
                      (f" {fmt_n(record['prompt_tokens']):>7}", "blue"),
                      (f" {fmt_n(record['completion_tokens']):>6}", "blue"),
                      (f" {reuse:>6}", "live"),
                      (f" {tps:>8}", "orange"),
                      (f" {prefill:>8}", "orange")])
    return box("RECENT REQUESTS  (PgUp/PgDn)", inner, width)


def render(state: dict, width: int, height: int, sel: dict | None = None) -> list[list[Seg]]:
    lines = header_section(state, width, sel)
    lines += system_section(state, width)
    lines += backends_section(state, width, sel)
    lines += models_section(state, width)
    fixed = len(lines)
    limit = max(1, min(12, height - fixed - 2))
    lines += recent_section(state, width, limit)
    return lines


def draw(screen, state: dict, colors: dict, sel: dict | None = None,
         actions: "Actions | None" = None, pending: tuple | None = None) -> None:
    height, width = screen.getmaxyx()
    screen.erase()
    for y, segs in enumerate(render(state, width - 1, height, sel)[:height - 1]):
        x = 0
        for text, role in segs:
            if x >= width - 1:
                break
            try:
                screen.addnstr(y, x, text, width - 1 - x, colors.get(role, 0))
            except curses.error:
                pass
            x += len(text)
    if pending:
        footer = f" {pending[1]}  [y/N] "
        attr = colors.get("err", 0) | curses.A_REVERSE
    else:
        status = actions.status() if actions else ""
        hint = f"  ▸ {sel['label']} [{sel['keys']}]" if sel else ""
        footer = f"{'⚡ ' + status + ' · ' if status else ''}q quit · Q stop gate · R restart panel · p pause · j/k select · PgUp/PgDn log{hint}"
        attr = curses.A_REVERSE
    try:
        screen.addnstr(height - 1, 0, footer, width - 1, attr)
    except curses.error:
        pass
    screen.refresh()


def flatten(segs: list[Seg]) -> str:
    return "".join(text for text, _ in segs)


def main() -> None:
    parser = argparse.ArgumentParser(description="model-gate monitoring panel")
    parser.add_argument("--url", default=os.environ.get("MODEL_GATE_URL", "http://127.0.0.1:9000"))
    parser.add_argument("--omlx-url", default="http://127.0.0.1:8001")
    parser.add_argument("--gpu", action=argparse.BooleanOptionalAction, default=True,
                        help="real GPU%% via sudo powermetrics (по умолчанию вкл; --no-gpu выключает)")
    parser.add_argument("--once", action="store_true", help="single frame to stdout, no curses")
    args = parser.parse_args()

    if args.gpu:
        # NOPASSWD-правило в sudoers (см. README) отбывает здесь же; без него — как раньше
        probe = subprocess.run(["sudo", "-n", "powermetrics", "--samplers", "gpu_power,cpu_power", "-i", "100", "-n", "1"],
                               capture_output=True, timeout=15)
        if probe.returncode != 0:
            print("Need sudo for powermetrics…")
            if subprocess.run(["sudo", "-v"]).returncode != 0:
                print("sudo denied — GPU%% stays off")
                args.gpu = False

    poller = Poller(args.url, args.omlx_url, args.gpu)
    if args.once:
        thread = threading.Thread(target=poller.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not (
            poller.state["gate_stats"] and poller.state["sys"] and poller.state["cpu"] is not None
        ):
            time.sleep(0.3)
        for segs in render(poller.state, 110, 60):
            print(flatten(segs))
        poller.stop()
        return

    def wrapper(screen):
        curses.curs_set(0)
        screen.nodelay(True)
        colors: dict[str, int] = {}
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            palette = GRUVBOX_256 if curses.COLORS >= 256 else GRUVBOX_16
            for role, color in palette.items():
                if role == "fg":
                    continue
                index = list(palette).index(role) + 1
                curses.init_pair(index, color, -1)
                colors[role] = curses.color_pair(index) | (curses.A_BOLD if role in BOLD_ROLES else 0)
        poller.start()
        actions = Actions(args.url, args.omlx_url)
        paused, scroll, sel_index, pending = False, 0, 0, None
        while True:
            screen.timeout(500)
            key = screen.getch()
            if key in (ord("q"), 27):
                break
            if key == ord("p"):
                paused = not paused
            with poller.lock:
                state = dict(poller.state)
                recent = list((state.get("gate_stats") or {}).get("recent", []))
            targets = build_targets(state)
            sel_index = max(0, min(sel_index, len(targets) - 1))
            sel = targets[sel_index] if targets else None

            def backend_models(name: str) -> list[dict]:
                for entry in (state.get("gate_status") or {}).get("backends", []):
                    if entry["name"] == name:
                        return entry.get("models", [])
                return []

            if pending:
                if key == ord("y"):
                    pending[0]()
                pending = None
            elif key == ord("s") and sel and "s" in sel["keys"]:
                backend = sel["backend"]
                pending = (lambda b=backend: actions.stop(b), f"STOP backend {backend} (освободит RAM)")
            elif key == ord("w") and sel and "w" in sel["keys"]:
                actions.warm(sel["id"])
            elif key == ord("r") and sel and "r" in sel["keys"]:
                backend = sel["backend"]
                models = backend_models(backend)
                warm_model = next((m["id"] for m in models if m.get("active") or m.get("loaded") or m.get("running")), None)
                pending = (lambda b=backend, m=warm_model: actions.restart(b, m), f"RESTART backend {backend}")
            elif key == ord("x") and sel and "x" in sel["keys"]:
                actions.cancel(sel["id"])
            elif key == ord("u") and sel and "u" in sel["keys"]:
                api_key = (state.get("omlx") or {}).get("api_key", "")
                model_id = sel["id"]
                pending = (lambda m=model_id, k=api_key: actions.omlx_unload(m, k), f"UNLOAD omlx model {model_id[:34]}")
            elif key == ord("o") and sel and "o" in sel["keys"]:
                cli = (state.get("omlx") or {}).get("cli_prefix") or "/Applications/oMLX.app/Contents/MacOS/omlx-cli"
                pending = (lambda c=cli: actions.omlx_restart(c), "RESTART omlx SERVER (сбросит все модели)")
            elif key == ord("Q"):
                pending = (actions.stop_gate, "STOP model-gate (все ds4-серверы умрут)")
            elif key == ord("R"):
                # рестарт только панели (свежий код/палитра), gate не трогать
                poller.stop()
                curses.endwin()
                os.execv(sys.executable, [sys.executable, sys.argv[0], *sys.argv[1:]])
            if key == curses.KEY_NPAGE:
                scroll += 5
            elif key == curses.KEY_PPAGE:
                scroll = max(0, scroll - 5)
            elif key in (curses.KEY_DOWN, ord("j")):
                sel_index = min(sel_index + 1, max(0, len(targets) - 1))
            elif key in (curses.KEY_UP, ord("k")):
                sel_index = max(0, sel_index - 1)
            if not paused:
                state["_recent_view"] = recent[scroll:]
                draw(screen, state, colors, sel, actions, pending)
        poller.stop()

    curses.wrapper(wrapper)


if __name__ == "__main__":
    main()
