import contextlib
from email.message import Message
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import xdl


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.out = self.root / "downloads"
        self.item = {"id": "1", "url": "https://x.com/test/status/1",
                     "author": "test", "hasVideo": True}

    def run_main(self, *args):
        with patch.object(xdl, "DATA", self.data), \
                patch("sys.argv", ["xdl.py", "--out", str(self.out), *args]), \
                patch.object(xdl.time, "sleep"), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as result:
            xdl.main()
        return result.exception.code

    def seed(self, items, state):
        self.data.mkdir()
        (self.data / "bookmarks-latest.json").write_text(json.dumps({"items": items}))
        (self.data / "state.json").write_text(json.dumps(state))

    def test_dry_run_preserves_project_files(self):
        self.seed([], {})
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}

        def scrape(max_items, snapshot):
            self.assertNotEqual(snapshot.parent, self.data)
            snapshot.write_text("temporary snapshot")
            return [self.item]

        with patch.object(xdl, "scrape", side_effect=scrape), \
                patch.object(xdl, "download", return_value={"status": "downloaded", "file": None}) as download:
            self.assertEqual(self.run_main("--dry-run"), 0)
        self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, before)
        self.assertFalse(self.out.exists())
        self.assertTrue(download.call_args.args[2])

    def test_dry_run_does_not_create_project_directories(self):
        with patch.object(xdl, "scrape", return_value=[]):
            self.assertEqual(self.run_main("--dry-run"), 0)
        self.assertFalse(self.data.exists())
        self.assertFalse(self.out.exists())

    def test_failed_item_outside_snapshot_is_retried(self):
        self.seed([], {"1": {**self.item, "status": "failed", "attempts": 2}})
        with patch.object(xdl, "download", return_value={"status": "no_video"}) as download:
            self.assertEqual(self.run_main("--skip-scrape"), 1)
        download.assert_called_once_with({**self.item, "images": [], "videos": []}, self.out, False)
        entry = json.loads((self.data / "state.json").read_text())["1"]
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["attempts"], 3)
        self.assertTrue(entry["hasVideo"])

    def test_failed_item_in_snapshot_is_attempted_once(self):
        self.seed([self.item], {"1": {**self.item, "status": "failed", "attempts": 1}})
        with patch.object(xdl, "download", return_value={"status": "downloaded", "file": None}) as download:
            self.assertEqual(self.run_main("--skip-scrape"), 0)
            download.assert_called_once()
            self.assertEqual(self.run_main("--skip-scrape"), 0)
            download.assert_called_once()

    def test_old_failed_record_can_be_retried(self):
        self.seed([], {"1": {"url": self.item["url"], "author": "test",
                              "status": "failed", "attempts": 1}})
        with patch.object(xdl, "download", return_value={"status": "downloaded", "file": None}):
            self.assertEqual(self.run_main("--skip-scrape"), 0)

    def test_download_records_every_video_in_a_tweet(self):
        paths = [str(self.out / "first.mp4"), str(self.out / "second.mp4")]
        proc = subprocess.CompletedProcess([], 0, "\n".join(paths) + "\n", "")
        with patch.object(xdl.subprocess, "run", return_value=proc):
            result = xdl.download(self.item, self.out, False)
        self.assertEqual(result["files"], paths)
        self.assertEqual(result["file"], paths[-1])

    def test_download_uses_direct_video_urls_and_output_template(self):
        urls = ["https://video.twimg.com/v/1920x1080/first.mp4",
                "https://video.twimg.com/v/1280x720/second.mp4"]
        item = {**self.item, "videos": [
            {"id": str(index), "variants": [{"url": url, "bitrate": 2000,
                                            "width": 1920, "height": 1080}]}
            for index, url in enumerate(urls)]}
        proc = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(xdl.subprocess, "run", return_value=proc) as run:
            self.assertEqual(xdl.download(item, self.out, False)["status"], "downloaded")
        command = run.call_args.args[0]
        self.assertEqual(command[-len(urls):], urls)
        self.assertNotIn(item["url"], command)
        self.assertEqual(command[command.index("-o") + 1],
                         str(self.out / "test-1-%(id)s.%(ext)s"))

    def test_delivery_includes_all_videos_and_old_records(self):
        self.data.mkdir()
        self.out.mkdir()
        paths = [str(self.out / name) for name in ("first.mp4", "second.mp4", "old.mp4")]
        for path in paths:
            Path(path).write_bytes(b"video")
        state = {
            "1": {"status": "downloaded", "file": paths[1], "files": paths[:2]},
            "2": {"status": "downloaded", "file": paths[2]},
        }
        proc = subprocess.CompletedProcess([], 0, json.dumps({
            "status": "delivered", "phone_folder": "X download/test"}), "")
        with patch.object(xdl, "DATA", self.data), \
                patch.object(xdl.subprocess, "run", return_value=proc) as run:
            self.assertEqual(xdl.deliver_pending(state, False), (2, None))
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--") + 1:], paths)
        self.assertTrue(all(entry["delivered"] == "X download/test" for entry in state.values()))

    def test_image_download_uses_original_and_reuses_completed_file(self):
        self.out.mkdir()
        item = {**self.item, "images": ["https://pbs.twimg.com/media/sample?format=png&name=small"]}
        response = io.BytesIO(b"image data")
        response.headers = Message()
        response.headers["Content-Type"] = "image/png"
        with patch.object(xdl, "urlopen", return_value=response) as fetch:
            paths = xdl.download_images(item, self.out, False)
            self.assertEqual(xdl.download_images(item, self.out, False), paths)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0].full_url,
                         "https://pbs.twimg.com/media/sample?format=png&name=orig")
        self.assertEqual(Path(paths[0]).read_bytes(), b"image data")
        self.assertFalse(list(self.out.glob("*.part")))

    def test_images_are_simulated_without_network_or_files(self):
        item = {**self.item, "images": ["https://pbs.twimg.com/media/sample?format=jpg&name=small"]}
        with patch.object(xdl, "urlopen") as fetch:
            self.assertEqual(xdl.download_images(item, self.out, True), [])
        fetch.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_old_no_video_entry_gets_images_and_is_then_skipped(self):
        image = "https://pbs.twimg.com/media/sample?format=jpg&name=orig"
        self.seed([{**self.item, "hasVideo": False, "images": [image]}], {
            "1": {"status": "no_video", "attempts": 1}})
        photo = str(self.out / "sample.jpg")
        with patch.object(xdl, "download") as video, \
                patch.object(xdl, "download_images", return_value=[photo]) as images, \
                patch.object(xdl, "deliver_pending", return_value=(0, None)):
            self.assertEqual(self.run_main("--skip-scrape"), 0)
            self.assertEqual(self.run_main("--skip-scrape"), 0)
        video.assert_not_called()
        images.assert_called_once()
        entry = json.loads((self.data / "state.json").read_text())["1"]
        self.assertEqual(entry["status"], "downloaded")
        self.assertEqual(entry["files"], [photo])

    def test_adding_images_keeps_video_and_delivers_only_new_files(self):
        image = "https://pbs.twimg.com/media/sample?format=jpg&name=orig"
        old_video = str(self.out / "video.mp4")
        photo = str(self.out / "sample.jpg")
        self.seed([{**self.item, "images": [image]}], {
            "1": {"status": "downloaded", "attempts": 1, "file": old_video,
                  "delivered": "previous batch"}})
        self.out.mkdir()
        Path(old_video).write_bytes(b"video")
        Path(photo).write_bytes(b"image")
        delivery = subprocess.CompletedProcess([], 0, json.dumps({
            "status": "delivered", "phone_folder": "X download/images"}), "")
        with patch.object(xdl, "download") as video, \
                patch.object(xdl, "download_images", return_value=[photo]), \
                patch.object(xdl.subprocess, "run", return_value=delivery) as deliver:
            self.assertEqual(self.run_main("--skip-scrape"), 0)
            self.assertEqual(self.run_main("--skip-scrape"), 0)
        video.assert_not_called()
        deliver.assert_called_once()
        command = deliver.call_args.args[0]
        self.assertEqual(command[command.index("--") + 1:], [photo])
        entry = json.loads((self.data / "state.json").read_text())["1"]
        self.assertEqual(entry["files"], [old_video, photo])
        self.assertEqual(entry["delivered_files"], [old_video, photo])

    def test_image_failure_remains_retryable(self):
        item = {**self.item, "hasVideo": False,
                "images": ["https://pbs.twimg.com/media/sample?format=jpg&name=orig"]}
        self.seed([item], {})
        with patch.object(xdl, "urlopen", side_effect=URLError("connection lost")):
            self.assertEqual(self.run_main("--skip-scrape"), 1)
        entry = json.loads((self.data / "state.json").read_text())["1"]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("connection lost", entry["error"])
        self.assertFalse(list(self.out.glob("*.part")))


if __name__ == "__main__":
    unittest.main()
