"""mgpan — цветная TUI-панель мониторинга model-gate (raw ANSI, без зависимостей).

Gruvbox, логотип lynxy-dev, боксы: header+LIVE, SYSTEM (подбоксы RAM|GPU|CPU
с многоуровневыми графиками), BACKENDS (модели и метрики провайдеров),
МОДЕЛИ, RECENT. Источники: gate /admin/stats + /admin/status,
omlx /admin/api/stats, vm_stat/top/sysctl, опционально sudo powermetrics.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import tty
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

EIGHTHS = " ▁▂▃▄▅▆▇█"
HISTORY = 150
CHART_ROWS = 4

GRUVBOX_RGB = {
    # палитра темы lynxy-dev (gruvbox material) — цвета один в один с Pi
    "fg":     (0xEB, 0xDB, 0xB2),   # fg1
    "dim":    (0x92, 0x83, 0x74),   # gray
    "border": (0x66, 0x5C, 0x54),   # bg3 — рамки как в футере Pi
    "ok":     (0xB8, 0xBB, 0x26),   # green
    "err":    (0xFB, 0x49, 0x34),   # red
    "warn":   (0xFE, 0x80, 0x19),   # orange
    "orange": (0xFA, 0xBD, 0x2F),   # yellow — скорости
    "head":   (0x8E, 0xC0, 0x7C),   # aqua
    "live":   (0xD3, 0x86, 0x9B),   # purple
    "blue":   (0x83, 0xA5, 0x98),   # blue
}
BOLD_ROLES = ("head", "live")
# truecolor SGR: ncurses на macOS truecolor не умеет (нет setrgb в terminfo),
# поэтому рендер — raw ANSI, как у Pi; цвета не зависят от палитры терминала
SGR: dict[str, str] = {"": "\x1b[0m"}
SGR.update({role: ("\x1b[1m" if role in BOLD_ROLES else "") + f"\x1b[38;2;{r};{g};{b}m"
            for role, (r, g, b) in GRUVBOX_RGB.items()})

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
    # порядок обхода j/k = порядок отрисовки: боксы отсортированы по имени
    for backend in sorted((state.get("gate_status") or {}).get("backends", []),
                          key=lambda b: b["name"]):
        managed = backend.get("process") is not None
        targets.append({"kind": "backend", "backend": backend["name"], "managed": managed,
                        "label": f"backend {backend['name']}", "keys": "s r w" if managed else "o"})
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


def read_kv_dir() -> tuple[str, int] | None:
    """--kv-disk-dir и лимит из командной строки живого ds4-server."""
    raw = subprocess.run(["ps", "-axww", "-o", "command="], capture_output=True, text=True).stdout
    for line in raw.splitlines():
        cmd = line.strip()
        # бинарник обязан стоять в начале ps-строки (реальный запуск), иначе
        # поймаем любой процесс, у которого этот текст затесался в аргументах
        if not cmd.startswith("/") or "/ds4-server" not in cmd.split(" ", 1)[0]:
            continue
        match = re.search(r"--kv-disk-dir (\S+)", cmd)
        if not match:
            continue
        cap = re.search(r"--kv-disk-space-mb (\d+)", cmd)
        return match.group(1), int(cap.group(1)) * 1024 * 1024 if cap else None
    return None


def dir_size(path: str) -> int:
    total = 0
    with os.scandir(path) as entries:
        for entry in entries:
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                pass
    return total


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
            "gpu_pct": None, "gpu_watts": None, "sys_watts": None, "gpu_temp": None,
            "gpu_note": "off (--no-gpu)" if not gpu else "",
            "kv_disk": None,
            "hist": {k: deque(maxlen=HISTORY) for k in ("cpu", "mem", "pool", "gpu", "watts", "temp")},
            "rates": {"req_min": 0},
        }
        self._gpu_proc: subprocess.Popen | None = None
        self._gpu_log = None
        self._kv_dir: tuple[str, int] | None = None
        self._stop = threading.Event()

    def _start_gpu(self) -> None:
        try:
            self._gpu_log = open("/tmp/mgpan-gpu.log", "w")
            self._gpu_proc = subprocess.Popen(
                # без -n: на macOS 26 "-n 0" больше не «бесконечно», а «ноль сэмплов»
                ["sudo", "-n", "powermetrics", "--samplers",
                 "gpu_power,cpu_power,battery,thermal", "-i", "1000"],
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
            buffer = ""
            if busy_pct is None:
                continue
            with self.lock:
                self.state["gpu_pct"] = busy_pct
                self.state["gpu_watts"] = watts
                self.state["hist"]["gpu"].append(busy_pct)
                self.state["gpu_note"] = ""

    # Полное питание системы powermetrics на macOS 26 не отдаёт (battery-самплер
    # молчит), температуры — тоже. Берём из первых рук:
    #  * ioreg AppleSmartBattery PowerTelemetryData — потребление всей машины, mW;
    #  * macmon pipe — GPU-температура через IOReport без sudo.
    def _read_system_watts(self) -> float | None:
        try:
            out = subprocess.run(["ioreg", "-rn", "AppleSmartBattery"],
                                 capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        telemetry = re.search(r'"PowerTelemetryData" = \{([^}]*)\}', out)
        if not telemetry:
            return None
        blob = telemetry.group(1)
        match = re.search(r'"SystemPowerIn"=(-?\d+)', blob)
        if '"ExternalConnected" = Yes' in out and match:
            return max(0.0, int(match.group(1)) / 1000)
        match = re.search(r'"BatteryPower"=(-?\d+)', blob)
        if match:
            value = int(match.group(1))
            if value >= 2**63:  # ioreg печатает отрицательные как unsigned 2^64-x
                value -= 2**64
            return abs(value) / 1000
        return None

    def _pump_macmon(self) -> None:
        binary = shutil.which("macmon")
        if not binary:
            return  # панель работает и без него: просто не будет бокса TEMP
        try:
            proc = subprocess.Popen([binary, "pipe", "-i", "1000"], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True)
        except OSError:
            return
        while not self._stop.is_set():
            line = proc.stdout.readline()
            if not line:
                break
            try:
                temp = (json.loads(line).get("temp") or {}).get("gpu_temp_avg")
            except ValueError:
                continue
            if temp is None:
                continue
            with self.lock:
                # пишем каждый сэмпл (не только при изменении): спарклайны всегда полные
                self.state["hist"]["temp"].append(temp)
                self.state["gpu_temp"] = round(temp, 1)
        proc.terminate()

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
        threading.Thread(target=self._pump_macmon, daemon=True).start()
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
            if cycle % 3 == 0:  # каталог KV-кэша: путь ловим один раз, размер — периодически
                try:
                    if self._kv_dir is None:
                        self._kv_dir = read_kv_dir()
                    if self._kv_dir:
                        path, cap = self._kv_dir
                        used = dir_size(path)
                        with self.lock:
                            self.state["kv_disk"] = {"path": path, "bytes": used, "cap": cap}
                except OSError:
                    self._kv_dir = None
            cpu = read_cpu_busy()
            with self.lock:
                self.state["cpu"] = cpu
                self.state["hist"]["cpu"].append(cpu)
            sys_watts = self._read_system_watts()
            if sys_watts is not None:
                with self.lock:
                    self.state["hist"]["watts"].append(sys_watts)
                    self.state["sys_watts"] = sys_watts
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
    title = title[:max(0, width - 6)]  # заголовок не должен разрывать рамку
    lines: list[list[Seg]] = [[("╭─ ", "border"), (title, "head"),
                               (" " + "─" * max(0, width - len(title) - 5), "border"), ("╮", "border")]]
    for segs in inner:
        used = sum(len(text) for text, _ in segs)
        if used > inner_width:  # узкий терминал: режем строку, а не ломаем сетку
            clipped: list[Seg] = []
            left = inner_width
            for text, role in segs:
                if left <= 0:
                    break
                clipped.append((text[:left], role))
                left -= len(text)
            segs = clipped
            used = inner_width
        pad = max(0, inner_width - used)
        lines.append([("│ ", "border")] + segs + [(" " * pad, "border"), (" │", "border")])
    lines.append([("╰" + "─" * (width - 2) + "╯", "border")])
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
        errors = sum(m.get("errors", 0) for m in stats.get("models", {}).values())
        served = sum(m.get("requests", 0) for m in stats.get("models", {}).values())
        # все показатели одной строкой: подписи тусклые, значения цветные
        info = [[
            ("up ", "dim"), (fmt_g(stats.get("uptime_s")), "fg"),
            (" · ", "dim"), ("active ", "dim"), (str(active), "live" if active else "dim"),
            (" · ", "dim"), ("queue ", "dim"), (str(queue), "warn" if queue else "dim"),
            (" · ", "dim"), (f"{state['rates']['req_min']} req/min", "blue"),
            (" · ", "dim"), ("err ", "dim"),
            (f"{errors}/{served}", "err" if errors else "dim"),
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
                       ("  " + spark(watts_hist, ceiling, 10), "warn")]]
        power_box = box("POWER", power_rows, right_width)

    # боксы правого столбца стоят вплотную друг к другу; если стек ниже лого-рамки — добор снизу
    stack = power_box + live_box
    height = max(3 + len(stack), len(logo_inner) + 2)
    right += stack + [[("", "")]] * max(0, height - 3 - len(stack))
    logo_inner += [[]] * (height - 2 - len(logo_inner))
    logo_box: list[list[Seg]] = [[("╭" + "─" * (LOGO_WIDTH + 2) + "╮", "border")]]
    for segs in logo_inner:
        used = sum(len(text) for text, _ in segs)
        logo_box.append([("│ ", "border")] + segs + [(" " * (LOGO_WIDTH - used), "border"), (" │", "border")])
    logo_box.append([("╰" + "─" * (LOGO_WIDTH + 2) + "╯", "border")])

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
    temp = state.get("gpu_temp")
    # четвёртый подбокс — температура GPU (самплер thermal, ключи TG0*)
    sub_width = (inner_width - 3) // (4 if temp is not None else 3)
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
    ] + ([sub("TEMP", f"{temp:.0f}°C", hist["temp"], 110.0, (temp or 0) / 110)] if temp is not None else []) + [
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


def backends_section(state: dict, width: int, sel: dict | None = None,
                     vertical: bool = False) -> list[list[Seg]]:
    """Каждый провайдер — отдельный бокс: горизонтально |ds4|omlx| или стеком (узкий режим)."""
    status = state.get("gate_status") or {}
    stats = state.get("gate_stats") or {}
    metrics = stats.get("backend_metrics", {})
    omlx = state.get("omlx") or {}
    sub_width = width - 4 if vertical else (width - 4 - 1) // 2
    now = time.time()
    groups: list[tuple[str, list[list[Seg]]]] = []
    for backend in sorted(status.get("backends", []), key=lambda b: b["name"]):
        name = backend["name"]
        up = backend.get("ready")
        process = backend.get("process") or {}
        cursor = sel and sel["kind"] == "backend" and sel["backend"] == name
        # имя провайдера уже в заголовке бокса — внутри только статус и pid
        head: list[Seg] = [(f"{'▸' if cursor else ''}{'●' if up else '○'} {'up' if up else 'down'}", "ok" if up else "err")]
        if process.get("pid"):
            head.append((f" pid {process['pid']}", "dim"))
        inner: list[list[Seg]] = [head]
        if name == "omlx" and omlx:
            inner.append([("prefill {} · gen {} t/s".format(omlx.get("avg_prefill_tps", "?"), omlx.get("avg_generation_tps", "?")), "orange")])
            inner.append([("cache {}% · up {}".format(omlx.get("cache_efficiency", "?"), fmt_g(omlx.get("uptime_seconds"))), "dim")])
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
                inner.append([(" · ".join(bits), "orange")])
            # кэш-хит и аптайм считаем по запросам гейта к этому бэкенду
            totals = [(m["prompt_tokens"], m.get("cached_tokens") or 0)
                      for key, m in stats.get("models", {}).items() if key.startswith(name + "/")]
            prompt = sum(p for p, _ in totals)
            cached = sum(c for _, c in totals)
            tail: list[Seg] = []
            if prompt:
                tail.append((f"cache {100 * cached / prompt:.0f}%", "dim"))
            # KV-кэш на диске — добро ds4; предупреждаем, когда каталог подпёр к лимиту
            kv = state.get("kv_disk") or {} if name == "ds4" else {}
            if kv.get("bytes") is not None:
                used_g = kv["bytes"] / 2**30
                cap_g = kv["cap"] / 2**30 if kv.get("cap") else None
                text = f"kv {used_g:.0f}/{cap_g:.0f}G" if cap_g else f"kv {used_g:.0f}G"
                tail.append((text, "warn" if cap_g and used_g >= 0.9 * cap_g else "blue"))
            if process.get("started_ts"):
                tail.append((f"up {fmt_g(now - process['started_ts'])}", "dim"))
            if tail:
                line: list[Seg] = []
                for index, (text, role) in enumerate(tail):
                    if index:
                        line.append((" · ", "dim"))
                    line.append((text, role))
                inner.append(line)
        if not vertical:  # в узком режиме пустая строка между статусом и списком не нужна
            inner.append([("", "")])
        for model in backend.get("models", []):
            if model.get("active"):
                role, mark = "live", "▶"
            elif model.get("loaded") or model.get("running"):
                role, mark = "ok", "◆"
            else:
                role, mark = "dim", "·"
            inner.append([(f"{mark} {model['id'][:sub_width - 5]}", role)])
        groups.append((name, inner))
    if not groups:
        return box("BACKENDS", [[("no data from gate", "warn")]], width)
    if vertical:
        lines: list[list[Seg]] = []
        for name, inner in groups:
            lines += box(name, inner, width)
        return lines
    height = max(len(inner) for _, inner in groups)
    boxes = [box(name, inner + [[]] * (height - len(inner)), sub_width)
             for name, inner in groups]
    return box("BACKENDS", merge_boxes(boxes, gap=1), width)


def models_section(state: dict, width: int, compact: bool = False) -> list[list[Seg]]:
    stats = state.get("gate_stats") or {}
    models = stats.get("models", {})
    inner: list[list[Seg]] = []
    if not models:
        inner.append([("no traffic yet", "dim")])
    elif compact:
        # узкий режим: только главные колонки, модель во всю оставшуюся ширину
        name_w = max(6, width - 4 - 26)
        inner.append([(f"{'model':<{name_w}}", "dim"), (f"{'req':>6}", "dim"),
                      (f"{'in':>7}", "dim"), (f"{'out':>7}", "dim"), (f"{'t/s':>6}", "dim")])
        for key in sorted(models, key=lambda k: -models[k]["completion_tokens"]):
            model = models[key]
            avg_tps = model.get("avg_tps")
            short = key.replace("ds4/", "d:").replace("omlx/", "o:")
            role = "ok" if model["errors"] == 0 else "warn"
            inner.append([(f"{short[:name_w]:<{name_w}}", role),
                          (f"{model['requests']:>6}", "fg"),
                          (f"{fmt_n(model['prompt_tokens']):>7}", "fg"),
                          (f"{fmt_n(model['completion_tokens']):>7}", "fg"),
                          (f"{(f'{avg_tps:.0f}' if avg_tps else '—'):>6}", "orange")])
    else:
        inner.append([("model" + " " * 29, "dim"), (f"{'req':>5}", "dim"), (f"{'in':>8}", "dim"),
                      (f"{'out':>8}", "dim"), (f"{'cache':>6}", "dim"), (f"{'pre t/s':>8}", "dim"),
                      (f"{'gen t/s':>8}", "dim"), (f"{'last':>7}", "dim")])
        for key in sorted(models, key=lambda k: -models[k]["completion_tokens"]):
            model = models[key]
            cache = model.get("cache_hit_rate")
            avg_tps = model.get("avg_tps")
            last_elapsed = model.get("last_elapsed_s")
            prefill = model.get("last_prefill_tps")
            short = key.replace("ds4/", "d:").replace("omlx/", "o:")
            role = "ok" if model["errors"] == 0 else "warn"
            inner.append([(f"{short[:30]:<34}", role),
                          (f"{model['requests']:>5}", "fg"),
                          (f"{fmt_n(model['prompt_tokens']):>8}", "fg"),
                          (f"{fmt_n(model['completion_tokens']):>8}", "fg"),
                          (f"{(f'{cache:.0f}%' if cache is not None else '—'):>6}", "fg"),
                          (f"{(f'{prefill:.0f}' if prefill else '—'):>8}", "orange"),
                          (f"{(f'{avg_tps:.1f}' if avg_tps else '—'):>8}", "fg"),
                          (f"{(f'{last_elapsed:.1f}s' if last_elapsed else '—'):>7}", "dim")])
    return box("MODELS (since gate start)", inner, width)


def recent_section(state: dict, width: int, limit: int, compact: bool = False) -> list[list[Seg]]:
    stats = state.get("gate_stats") or {}
    recent = state.get("_recent_view", stats.get("recent", []))[:limit]
    inner: list[list[Seg]] = []
    if not recent:
        inner.append([("no traffic yet", "dim")])
        return box("RECENT" if compact else "RECENT REQUESTS  (PgUp/PgDn)", inner, width)
    if compact:
        # без колонки статуса: имя модели зелёное при 200, красное при ошибке;
        # dur убран — cache-хит по каждому запросу важнее длительности
        name_w = max(6, width - 4 - 34)
        inner.append([(f"{'hh:mm':<6}", "dim"), (f"{'model':<{name_w}}", "dim"),
                      (f"{'cache':>5}", "live"), (f"{'ttft':>6}", "dim"),
                      (f"{'out':>6}", "blue"),
                      (f"{'t/s':>5}", "orange"), (f"{'pre':>6}", "orange")])
        for record in recent:
            stamp = datetime.fromtimestamp(record["ts"]).strftime("%H:%M")
            if record.get("disconnected"):
                role = "warn"
            elif record["status"] >= 400:
                role = "err"
            else:
                role = "ok"
            tps = f"{record['tps']:.0f}" if record.get("tps") else "—"
            prefill = f"{record['prefill_tps']:.0f}" if record.get("prefill_tps") else "—"
            prompt, cached = record.get("prompt_tokens") or 0, record.get("cached_tokens") or 0
            reuse = f"{100 * cached / prompt:.0f}%" if prompt and cached else "—"
            ttft = record.get("ttft_s")
            inner.append([(f"{stamp:<6}", "dim"),
                          (f"{record['model'][:name_w]:<{name_w}}", role),
                          (f"{reuse:>5}", "live"),
                          (f"{(f'{ttft:.1f}s' if ttft else '—'):>6}", "dim"),
                          (f"{fmt_n(record['completion_tokens']):>6}", "blue"),
                          (f"{tps:>5}", "orange"),
                          (f"{prefill:>6}", "orange")])
        while len(inner) < limit + 1:  # добиваем пустыми строками — бокс до футера
            inner.append([("", "")])
        return box("RECENT", inner, width)
    # колонки: каждая своим цветом — время тусклое, модель обычная, статус
    # по результату, токены синие, кэш розовый, скорости оранжевые
    inner.append([(f"{'time':<9}", "dim"), (f" {'model':<24}", "dim"), (" " + f"{'code':>4}", "dim"),
                  (" " + f"{'dur':>7}", "dim"), (" " + f"{'ttft':>5}", "dim"),
                  (f" {'in':>7}", "blue"), (f" {'out':>6}", "blue"),
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
        ttft = record.get("ttft_s")
        # ttft подсвечиваем по факту отзывчивости: зелёный <1с, янтарный <3с
        ttft_role = "dim" if ttft is None else "ok" if ttft < 1 else "orange" if ttft < 3 else "warn"
        inner.append([(f"{stamp:<9}", "dim"),
                      (f" {record['model'][:24]:<24}", "fg"),
                      (f"{flag:>5}", role),
                      (f" {record['elapsed_s']:>6.2f}s", "fg"),
                      (f" {(f'{ttft:.2f}s' if ttft else '—'):>5}", ttft_role),
                      (f" {fmt_n(record['prompt_tokens']):>7}", "blue"),
                      (f" {fmt_n(record['completion_tokens']):>6}", "blue"),
                      (f" {reuse:>6}", "live"),
                      (f" {tps:>8}", "orange"),
                      (f" {prefill:>8}", "orange")])
    while len(inner) < limit + 1:  # добиваем пустыми строками — бокс до футера
        inner.append([("", "")])
    return box("RECENT REQUESTS  (PgUp/PgDn)", inner, width)


def spark(values, ceiling: float, n: int) -> str:
    """Однострочный спарклайн из последних n значений."""
    tail = list(values)[-n:] or [0.0]
    return "".join(EIGHTHS[max(0, min(8, round(v / max(ceiling, 1e-9) * 8)))] for v in tail)


def compact_header_section(state: dict, width: int, sel: dict | None = None) -> list[list[Seg]]:
    """Узкий режим: лого рисуется не нужен — только статус, энергопотребление и LIVE."""
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
    active = status.get("active", 0)
    queue = status.get("waiting_exclusive", 0)
    errors = sum(m.get("errors", 0) for m in stats.get("models", {}).values())
    served = sum(m.get("requests", 0) for m in stats.get("models", {}).values())
    info = [[
        (f"up {fmt_g(stats.get('uptime_s'))}", "fg"), (" · ", "dim"),
        (str(active), "live" if active else "dim"), ("act", "dim"), (" · ", "dim"),
        (str(queue), "warn" if queue else "dim"), ("q", "dim"), (" · ", "dim"),
        (f"{state['rates']['req_min']}/min", "blue"), (" · ", "dim"),
        (f"{errors}err", "err" if errors else "dim"), (f"/{served}", "dim"),
    ]]
    watts = state.get("sys_watts")
    gpu_pct = state.get("gpu_pct")
    temp = state.get("gpu_temp")
    power_line: list[Seg] = [("gen ", "dim"), (f"{live_tps:.0f}", "orange"), (" · pre ", "dim"),
                             (f"{prefill:.0f} t/s", "orange")]
    # хвост со всякой всячиной добавляем, пока влезает в ширину
    used = sum(len(text) for text, _ in power_line)
    for extra in (
        [(" · ", "dim"), (f"⚡{watts:.0f}W", "warn")] if watts is not None else [],
        [(" · ", "dim"), (f"{temp:.0f}°C", "live")] if temp is not None else [],
        [(" · ", "dim"), (f"{gpu_pct:.0f}%", "orange")] if gpu_pct is not None else [],
    ):
        cost = sum(len(text) for text, _ in extra)
        if used + cost <= width - 4:
            power_line += extra
            used += cost
    info.append(power_line)
    lines = box("model-gate · -lynxy-dev-", info, width)

    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time()) % 10]
    live_inner: list[list[Seg]] = []
    if not inflight:
        live_inner = [[(f"{spinner} idle", "dim")]]
    for entry in inflight[:3]:
        elapsed, ttft = entry["elapsed_s"], entry["ttft_s"]
        approx = entry.get("approx_completion_tokens") or 0
        gen_time = (elapsed - ttft) if ttft else 0
        tps = (approx / gen_time) if approx and gen_time > 0.3 else None
        cursor = sel and sel["kind"] == "inflight" and sel["id"] == entry["id"]
        segs: list[Seg] = [("▸" if cursor else " ", "live"), (f"{spinner} ", "live"),
                           (f"{entry['model'][:12]} ", "fg"),
                           ("gen" if ttft else "pre", "live" if ttft else "orange"),
                           (f" {elapsed:.0f}s", "dim")]
        if tps:
            segs.append((f" {tps:.0f}t/s", "orange"))
        live_inner.append(segs)
    lines += box("LIVE", live_inner, width)
    return lines


def compact_system_section(state: dict, width: int) -> list[list[Seg]]:
    """Узкий режим: по однострочному спарклайну на метрику вместо четырёхстрочных графиков."""
    hist = state["hist"]
    sysinfo = state.get("sys") or {}
    cpu = state.get("cpu")
    gpu_pct = state.get("gpu_pct")
    pool = ((state.get("omlx") or {}).get("active_models") or {})
    n = width - 4 - 9
    rows: list[list[Seg]] = []
    if cpu is not None:
        rows.append([("cpu ", "dim"), (f"{cpu:>3.0f}% ", "fg"), (spark(hist["cpu"], 100.0, n), "blue")])
    if gpu_pct is not None:
        rows.append([("gpu ", "dim"), (f"{gpu_pct:>3.0f}% ", "fg"), (spark(hist["gpu"], 100.0, n), "orange")])
    elif pool.get("model_memory_max"):
        pool_frac = pool["model_memory_used"] / pool["model_memory_max"]
        rows.append([("pool", "dim"), (f"{pool_frac * 100:>3.0f}% ", "fg"),
                     (spark(hist["pool"], 1.0, n), "orange")])
    mem_frac = sysinfo.get("mem_frac")
    if mem_frac is not None:
        rows.append([("ram ", "dim"), (f"{mem_frac * 100:>3.0f}% ", "fg"), (spark(hist["mem"], 1.0, n), "live")])
    temp = state.get("gpu_temp")
    if temp is not None:
        rows.append([("tmp ", "dim"), (f"{temp:>3.0f}° ", "fg"), (spark(hist["temp"], 110.0, n), "warn")])
    watts = state.get("sys_watts")
    if watts is not None:
        rows.append([("pwr ", "dim"), (f"{watts:>3.0f}W ", "fg"), (spark(hist["watts"], 150.0, n), "warn")])
    if not rows:
        rows = [[("no data", "dim")]]
    return box("SYSTEM", rows, width)


def render(state: dict, width: int, height: int, sel: dict | None = None,
           compact: bool = False) -> list[list[Seg]]:
    if compact:
        lines = compact_header_section(state, width, sel)
        lines += compact_system_section(state, width)
        lines += backends_section(state, width, sel, vertical=True)
        lines += models_section(state, width, compact=True)
        fixed = len(lines)
        limit = max(1, height - fixed - 4)  # RECENT растягивается до самого футера
        lines += recent_section(state, width, limit, compact=True)
        return lines
    lines = header_section(state, width, sel)
    lines += system_section(state, width)
    lines += backends_section(state, width, sel)
    lines += models_section(state, width)
    fixed = len(lines)
    limit = max(1, height - fixed - 4)  # RECENT растягивается до самого футера
    lines += recent_section(state, width, limit)
    return lines


class Tty:
    """Raw-ANSI рендер: alt-screen, truecolor, сырой ввод — как у Pi, без ncurses."""

    def __enter__(self) -> "Tty":
        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
        sys.stdout.flush()
        return self

    def __exit__(self, *exc) -> None:
        sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def size(self) -> tuple[int, int]:
        try:
            rows, cols = struct.unpack("HH", fcntl.ioctl(self.fd, termios.TIOCGWINSZ, b"\x00" * 8)[:4])
            return int(rows), int(cols)
        except OSError:
            fallback = shutil.get_terminal_size()
            return fallback.lines, fallback.columns

    def paint(self, lines: list[list[Seg]], footer: str, footer_bold: bool = False) -> None:
        buf = ["\x1b[H"]
        prev = None
        for segs in lines:
            for text, role in segs:
                sgr = SGR.get(role, "")
                if sgr != prev:
                    buf.append(sgr)
                    prev = sgr
                buf.append(text)
            buf.append("\x1b[0m\x1b[K\r\n")
            prev = None
        buf.append("\x1b[7m" + ("\x1b[1m" if footer_bold else "") + footer + "\x1b[0m\x1b[K\x1b[J")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    def key(self, timeout: float) -> str | None:
        if not select.select([sys.stdin], [], [], timeout)[0]:
            return None
        data = os.read(self.fd, 16)
        if data.startswith(b"\x1b["):
            return {"A": "UP", "B": "DOWN", "5~": "PgUp", "6~": "PgDn"}.get(data[2:].decode("ascii", "ignore"))
        if data == b"\x1b":
            return "ESC"
        return chr(data[0]) if data else None


def draw(tty_: Tty, state: dict, sel: dict | None = None,
         actions: "Actions | None" = None, pending: tuple | None = None,
         compact: bool = False) -> None:
    height, width = tty_.size()
    lines = render(state, width - 1, height, sel, compact)[:height - 1]
    if pending:
        footer = f" {pending[1]}  [y/N] "
    else:
        status = actions.status() if actions else ""
        hint = f"  ▸ {sel['label']} [{sel['keys']}]" if sel else ""
        if compact:
            footer = f"{'⚡' + status + ' · ' if status else ''}q Q R p ↑↓ PgUp c{hint}"
        else:
            footer = f"{'⚡ ' + status + ' · ' if status else ''}q quit · Q stop gate · R restart panel · p pause · ↑/↓ select · PgUp/PgDn log{hint}"
    tty_.paint(lines, footer[:width - 1], footer_bold=bool(pending))


def flatten(segs: list[Seg]) -> str:
    return "".join(text for text, _ in segs)


def main() -> None:
    parser = argparse.ArgumentParser(description="model-gate monitoring panel")
    parser.add_argument("--url", default=os.environ.get("MODEL_GATE_URL", "http://127.0.0.1:9000"))
    parser.add_argument("--omlx-url", default="http://127.0.0.1:8001")
    parser.add_argument("--gpu", action=argparse.BooleanOptionalAction, default=True,
                        help="real GPU%% via sudo powermetrics (по умолчанию вкл; --no-gpu выключает)")
    parser.add_argument("--compact", action="store_true",
                        help="узкий режим (боковая панель, телефон); авто при ширине <80, клавиша c переключает")
    parser.add_argument("--once", action="store_true", help="single frame to stdout, no TUI")
    args = parser.parse_args()

    if args.gpu:
        # NOPASSWD-правило в sudoers (см. README) отбывает здесь же; без него — как раньше
        probe = subprocess.run(["sudo", "-n", "powermetrics", "--samplers",
                                "gpu_power,cpu_power,battery,thermal", "-i", "100", "-n", "1"],
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
        for segs in render(poller.state, 46 if args.compact else 110, 60, compact=args.compact):
            print(flatten(segs))
        poller.stop()
        return

    actions = Actions(args.url, args.omlx_url)
    paused, scroll, sel_index, pending, restart = False, 0, 0, None, False
    with Tty() as tty_:
        compact = args.compact or tty_.size()[1] < 80
        prev_width = tty_.size()[1]
        poller.start()
        while True:
            key = tty_.key(0.5)
            _, width = tty_.size()
            if width != prev_width:
                prev_width = width
                if not args.compact:
                    # терминал поменяли — режим пересчитывается по новой ширине
                    compact = width < 80
            if key in ("q", "ESC"):
                break
            if key == "p":
                paused = not paused
            if key == "c":
                compact = not compact
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
                if key == "y":
                    pending[0]()
                pending = None
            elif key == "s" and sel and "s" in sel["keys"]:
                backend = sel["backend"]
                pending = (lambda b=backend: actions.stop(b), f"STOP backend {backend} (освободит RAM)")
            elif key == "w" and sel and "w" in sel["keys"]:
                models = backend_models(sel["backend"])
                warm_model = next((m["id"] for m in models if m.get("active") or m.get("loaded") or m.get("running")),
                                  models[0]["id"] if models else None)
                if warm_model:
                    actions.warm(warm_model)
            elif key == "r" and sel and "r" in sel["keys"]:
                backend = sel["backend"]
                models = backend_models(backend)
                warm_model = next((m["id"] for m in models if m.get("active") or m.get("loaded") or m.get("running")), None)
                pending = (lambda b=backend, m=warm_model: actions.restart(b, m), f"RESTART backend {backend}")
            elif key == "x" and sel and "x" in sel["keys"]:
                actions.cancel(sel["id"])
            elif key == "o" and sel and "o" in sel["keys"]:
                cli = (state.get("omlx") or {}).get("cli_prefix") or "/Applications/oMLX.app/Contents/MacOS/omlx-cli"
                pending = (lambda c=cli: actions.omlx_restart(c), "RESTART omlx SERVER (сбросит все модели)")
            elif key == "Q":
                pending = (actions.stop_gate, "STOP model-gate (все ds4-серверы умрут)")
            elif key == "R":
                restart = True
                break
            if key == "PgUp":
                scroll += 5
            elif key == "PgDn":
                scroll = max(0, scroll - 5)
            elif key in ("DOWN", "j"):
                sel_index = min(sel_index + 1, max(0, len(targets) - 1))
            elif key in ("UP", "k"):
                sel_index = max(0, sel_index - 1)
            if not paused:
                state["_recent_view"] = recent[scroll:]
                draw(tty_, state, sel, actions, pending, compact)
        poller.stop()

    if restart:
        # рестарт только панели (свежий код/палитра), gate не трогать
        os.execv(sys.executable, [sys.executable, sys.argv[0], *sys.argv[1:]])


if __name__ == "__main__":
    main()
