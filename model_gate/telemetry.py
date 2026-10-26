"""Read-only backend measurements. The panel only talks to the gate."""
import json
import platform
from pathlib import Path
import re
import shutil
import subprocess


def collect_host():
    host = {'kind': 'host', 'sys': {}, 'cpu': None, 'gpu_pct': None,
            'gpu_watts': None, 'sys_watts': None, 'gpu_temp': None,
            'gpu_note': 'unavailable'}
    if platform.system() != 'Darwin':
        return host
    try:
        raw = subprocess.run(['vm_stat'], capture_output=True, text=True, timeout=3).stdout
        page_match = re.search(r'page size of (\d+)', raw)
        pages = {m.group(1).lower(): int(m.group(2))
                 for m in re.finditer(r'Pages (\w+(?: \w+)*):\s+(\d+)\.', raw)}
        total = int(subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True,
                                   text=True, timeout=3).stdout.strip())
        page = int(page_match.group(1)) if page_match else 4096
        free = pages.get('free', 0) * page
        host['sys'] = {'mem_total': total, 'mem_used': total-free,
                       'mem_wired': pages.get('wired down', 0)*page,
                       'mem_compressed': pages.get('occupied by compressor', 0)*page,
                       'mem_frac': (total-free)/total if total else None,
                       'swap': _swap_usage()}
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        raw = subprocess.run(['top', '-l', '2', '-n', '0', '-s', '1'],
                             capture_output=True, text=True, timeout=4).stdout
        samples = re.findall(r'CPU usage:\s*[\d.]+% user,\s*[\d.]+% sys,\s*([\d.]+)% idle', raw)
        if samples: host['cpu'] = 100.0-float(samples[-1])
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    binary = shutil.which('macmon')
    if binary:
        try:
            raw = subprocess.run([binary, 'pipe', '-s', '1', '-i', '250'],
                                 capture_output=True, text=True, timeout=3).stdout
            sample = json.loads(raw.splitlines()[-1])
            host['gpu_pct'] = round(float(sample.get('gpu_active_ratio', 0))*100, 1)
            host['gpu_watts'] = sample.get('gpu_power')
            host['gpu_temp'] = (sample.get('temp') or {}).get('gpu_temp_avg')
            host['sys_watts'] = sample.get('all_power')
            host['gpu_note'] = ''
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            pass
    return host


def _swap_usage():
    try:
        raw = subprocess.run(['sysctl', '-n', 'vm.swapusage'], capture_output=True,
                             text=True, timeout=3).stdout
        values = {k: float(v) * (1024**{'K':1,'M':2,'G':3}[unit])
                  for k, v, unit in re.findall(r'(total|used|free) = ([\d.]+)([KMG])', raw)}
        return [int(values.get('used', 0)), int(values.get('total', 0))]
    except (OSError, ValueError, subprocess.SubprocessError):
        return [0, 0]


def collect(backends, processes, open_url):
    result = {}
    for name, backend in backends.items():
        item = {'kind': backend.kind, 'metrics': None, 'kv_disk': None, 'rss_bytes': None}
        process = processes.get(name)
        if process:
            try:
                rss = subprocess.run(['ps', '-p', str(process['pid']), '-o', 'rss='],
                                     capture_output=True, text=True, timeout=2)
                item['rss_bytes'] = int(rss.stdout.strip()) * 1024
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            command = backend.command_for(process['model']) or []
            if '--kv-disk-dir' in command:
                try:
                    path = Path(command[command.index('--kv-disk-dir') + 1])
                    size = sum(p.stat().st_size for p in path.iterdir() if p.is_file() and not p.is_symlink())
                    cap = int(command[command.index('--kv-disk-space-mb') + 1]) * 1024**2 if '--kv-disk-space-mb' in command else None
                    item['kv_disk'] = {'path': str(path), 'bytes': size, 'cap': cap}
                except (OSError, ValueError, IndexError):
                    pass
        if backend.kind == 'omlx':
            try:
                with open_url(backend.base_url + '/admin/api/stats', timeout=1) as response:
                    item['metrics'] = json.loads(response.read())
            except (OSError, ValueError):
                pass
        result[name] = item
    result['_host'] = collect_host()
    return result
