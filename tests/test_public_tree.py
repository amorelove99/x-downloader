import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('public_tree', Path(__file__).resolve().parents[1] / 'scripts/check_public_tree.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class PublicTreeTests(unittest.TestCase):
    def test_safe_source_and_example_are_accepted(self):
        self.assertEqual(guard.violations('xdownload/state.py', b'ROOT = Path.home()'), [])
        self.assertEqual(guard.violations('examples/nas.example.json', b'{"mount_root": "/Volumes/MyMediaShare"}'), [])

    def test_private_files_and_unreviewed_roots_are_rejected(self):
        for name in ('data/state.json', 'downloads/a.mp4', '.env', 'build/lib/xdl.py', 'notes.md', 'xdownload/bark.json'):
            with self.subTest(name=name):
                self.assertTrue(guard.violations(name, b'{}'))

    def test_credentials_and_home_paths_are_rejected_without_echoing_values(self):
        for value in ('gh' + 'p_' + 'a' * 36, '/' + 'Users/example/private', '-----BEGIN ' + 'PRIVATE KEY-----'):
            rules = guard.violations('xdownload/state.py', value.encode())
            self.assertTrue(rules)
            self.assertNotIn(value, ' '.join(rules))


if __name__ == '__main__':
    unittest.main()
