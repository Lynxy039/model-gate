"""Read-only backend measurements. The panel only talks to the gate."""
import json
from pathlib import Path
import subprocess


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
    return result
