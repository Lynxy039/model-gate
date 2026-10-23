import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from model_gate.paths import Paths, atomic_write, instance_lock, private_dir, support_dir


class PathsTests(unittest.TestCase):
    def test_config_identity_is_stable_and_isolates_all_state(self):
        with tempfile.TemporaryDirectory() as tmp, patch('pathlib.Path.home', return_value=Path(tmp)):
            a = Paths(Path(tmp) / 'a.json')
            b = Paths(Path(tmp) / 'b.json')
            self.assertEqual(a.identity, Paths(Path(tmp) / '.' / 'a.json').identity)
            self.assertNotEqual(a.state, b.state)
            self.assertNotEqual(a.log, b.log)
            self.assertNotEqual(a.cache, b.cache)
            self.assertEqual(support_dir(), Path(tmp) / 'Library/Application Support/model-gate')

    def test_private_dirs_and_atomic_state_files_have_private_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = private_dir(Path(tmp) / 'state')
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            target = directory / 'selected'
            atomic_write(target, 'model-a')
            self.assertEqual(target.read_text(), 'model-a')
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_lock_is_exclusive_and_symlink_directories_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / 'state' / 'gate.lock'
            with instance_lock(lock, blocking=False):
                with self.assertRaises(BlockingIOError):
                    with instance_lock(lock, blocking=False): pass
            real = Path(tmp) / 'real'; real.mkdir()
            link = Path(tmp) / 'linked'; link.symlink_to(real)
            with self.assertRaisesRegex(ValueError, 'symlink directory'):
                private_dir(link)
