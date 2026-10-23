"""Private macOS paths; instance identity follows the configuration file."""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import tempfile


def support_dir():
    return Path.home() / 'Library' / 'Application Support' / 'model-gate'


def default_config():
    return support_dir() / 'config.json'


@dataclass(frozen=True)
class Paths:
    config: Path

    @property
    def identity(self):
        return hashlib.sha256(str(self.config.resolve()).encode()).hexdigest()[:16]

    @property
    def state(self):
        return support_dir() / 'instances' / self.identity

    @property
    def log(self):
        return Path.home() / 'Library' / 'Logs' / 'model-gate' / (self.identity + '.log')

    @property
    def cache(self):
        return Path.home() / 'Library' / 'Caches' / 'model-gate' / self.identity


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError(f'refusing symlink directory: {path}')
    path.chmod(0o700)
    return path


def atomic_write(path, text):
    private_dir(path.parent)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as output:
        name = output.name
        try:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            os.unlink(name)
            raise
    try:
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def instance_lock(path, blocking=True):
    private_dir(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
