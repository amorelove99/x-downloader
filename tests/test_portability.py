import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xdownload import cli, jobs, runner, state


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target, name, value in (
            (state, 'ROOT', self.root), (state, 'DATA', self.root / 'data'),
            (jobs, 'LOCK', self.root / 'lock'),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_fresh_install_defaults_to_local(self):
        self.assertEqual(state.settings()['delivery'], 'local')

    def test_doctor_without_nas_does_not_require_nas_tools_or_config(self):
        with patch.object(cli.shutil, 'which', side_effect=lambda name, **kw: '/bin/' + name if name != 'nas-deliver' else None), \
             patch.object(runner, 'nas_config', side_effect=AssertionError('NAS accessed in local mode')), \
             patch.object(jobs, 'schedule', return_value=0), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.doctor(), 0)
        self.assertNotIn('nas-deliver', output.getvalue())

    def test_project_nas_config_needs_no_external_skill(self):
        config = {'mount_root': str(self.root / 'mounted-share'), 'delivery_folder': 'Backups'}
        state.atomic(state.DATA / 'nas.json', config)
        self.assertEqual(runner.nas_config(), config)

    def test_nas_config_rejects_escape_from_mount(self):
        for folder in ('../elsewhere', '/elsewhere'):
            with self.subTest(folder=folder):
                state.atomic(state.DATA / 'nas.json', {'mount_root': str(self.root), 'delivery_folder': folder})
                with self.assertRaises(ValueError):
                    runner.nas_config()

    def test_saved_delivery_preference_wins_over_new_default(self):
        state.save_settings({'delivery': 'nas'})
        self.assertEqual(state.settings()['delivery'], 'nas')

    def test_broken_schedule_does_not_block_cli_help(self):
        state.DATA.mkdir()
        (state.DATA / 'schedule.json').write_text('{broken')
        result = subprocess.run([sys.executable, '-m', 'xdownload', '--help'],
                                env={**os.environ, 'XDL_HOME': str(self.root)},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('usage:', result.stdout)

    def test_schedule_label_stays_within_launchagents_and_retains_saved_identifier(self):
        for label in ('../../outside', '/elsewhere', 1):
            state.atomic(state.DATA / 'schedule.json', {'launchd_label': label})
            with self.assertRaises(ValueError):
                jobs.schedule_path()
        state.atomic(state.DATA / 'schedule.json', {'launchd_label': 'org.example.existing-task'})
        self.assertEqual(jobs.schedule_path().name, 'org.example.existing-task.plist')

    def test_local_result_page_opens_local_downloads(self):
        with patch.object(cli, 'choose', side_effect=['1', '0']), \
             patch.object(cli, 'open_folder') as opening, \
             contextlib.redirect_stdout(io.StringIO()):
            cli.result_page(0)
        opening.assert_called_once_with('local')

    def test_nas_month_symlink_cannot_escape_project(self):
        import time
        source = self.root / 'photo.jpg'
        source.write_bytes(b'photo')
        project = self.root / 'share' / 'X download'
        outside = self.root / 'outside'
        project.mkdir(parents=True)
        outside.mkdir()
        month = time.strftime('%Y-%m', time.localtime(source.stat().st_mtime))
        (project / month).symlink_to(outside, target_is_directory=True)
        done, errors, stalled = runner.copy_all([str(source)], project)
        self.assertFalse(done)
        self.assertTrue(errors)
        self.assertFalse(stalled)
        self.assertFalse((outside / source.name).exists())

    def test_nas_existing_part_symlink_is_never_written_or_removed(self):
        source = self.root / 'photo.jpg'
        source.write_bytes(b'photo')
        folder = self.root / 'share'
        folder.mkdir()
        outside = self.root / 'private.txt'
        outside.write_bytes(b'keep me')
        link = folder / 'photo.jpg.part'
        link.symlink_to(outside)
        result = runner.copy_verified(source, folder)
        self.assertEqual(result.read_bytes(), b'photo')
        self.assertEqual(outside.read_bytes(), b'keep me')
        self.assertTrue(link.is_symlink())


if __name__ == '__main__':
    unittest.main()
