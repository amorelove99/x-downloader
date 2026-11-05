#!/usr/bin/env python3
"""Download images and videos from X bookmarks using the logged-in ego-browser session.

Run: python3 -I xdl.py [--max 40] [--out DIR] [--skip-scrape] [--dry-run]
State lives in data/: state.json (per-tweet result), archive.txt (yt-dlp), runs.log.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
BIN_DIRS = [str(Path.home() / ".local/bin"), "/opt/homebrew/bin"]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_env():
    return {**os.environ, "PATH": os.pathsep.join(BIN_DIRS + [os.environ.get("PATH", "")])}


def scrape(max_items, out_json):
    print(f"[采集] 正在打开 X 收藏页，本次上限 {max_items} 条……", flush=True)
    out_json.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="xdl-progress-") as temporary:
        progress = Path(temporary) / "progress.log"
        progress.touch()
        script = ((ROOT / "scrape.mjs").read_text().replace("__MAX__", str(max_items))
                  .replace("__ACCOUNT_ONLY__", "false")
                  .replace("__OUT__", str(out_json)).replace("__PROGRESS__", json.dumps(str(progress))))
        # ego-browser buffers console output; read progress written by the browser script instead.
        with progress.open() as updates, ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(subprocess.run, ["ego-browser", "nodejs"], input=script,
                                     text=True, capture_output=True, timeout=300, env=run_env())
            while not future.done():
                print(updates.read(), end="", flush=True)
                time.sleep(0.2)
            print(updates.read(), end="", flush=True)
            proc = future.result()
    if not out_json.exists():
        raise SystemExit(f"scrape produced no output (exit {proc.returncode}): {(proc.stdout + proc.stderr)[-500:]}")
    result = json.loads(out_json.read_text())
    if result["status"] != "ok":
        raise SystemExit(f"scrape status: {result['status']} ({result.get('url')}); log in to X in ego lite and retry")
    return result["items"]


def download(item, out_dir, simulate):
    direct_urls = []
    output_template = out_dir / "%(uploader_id)s-%(id)s.%(ext)s"
    if item.get("videos"):
        from xdownload.media import select_video_urls
        direct_urls = select_video_urls(item, "1080")
        output_template = out_dir / f"{item['author']}-{item['id']}-%(id)s.%(ext)s"
    cmd = ["yt-dlp", "--ignore-config", "--no-playlist", "--no-progress", "--retries", "3",
           "--socket-timeout", "30", "-S", "res:1080", "--download-archive", str(DATA / "archive.txt"),
           "-o", str(output_template), "--print", "after_move:filepath"]
    if simulate:
        cmd += ["--simulate"]
    try:
        proc = subprocess.run(cmd + (direct_urls or [item["url"]]), capture_output=True, text=True, timeout=900, env=run_env())
    except subprocess.TimeoutExpired:
        return {"status": "failed", "error": "yt-dlp timed out after 900s"}
    err = proc.stderr.strip()
    if proc.returncode == 0:
        if "already been recorded in the archive" in proc.stdout + err:
            return {"status": "downloaded", "file": None, "note": "already in archive"}
        paths = proc.stdout.strip().splitlines()
        return {"status": "downloaded", "file": paths[-1] if paths else None, "files": paths}
    if "No video could be found in this tweet" in err or "Unsupported URL" in err:  # Unsupported: X Articles
        return {"status": "no_video"}
    return {"status": "failed", "error": err.splitlines()[-1][:300] if err else f"exit {proc.returncode}"}


def download_images(item, out_dir, simulate):
    paths = []
    for index, source in enumerate(item.get("images", []), 1):
        parts = urlsplit(source)
        if parts.scheme != "https" or parts.netloc != "pbs.twimg.com" or not parts.path.startswith("/media/"):
            raise ValueError(f"unexpected X image URL: {source}")
        image_format = parse_qs(parts.query).get("format", [Path(parts.path).suffix.lstrip(".") or "jpg"])[0]
        if image_format not in ("jpg", "jpeg", "png", "webp", "gif"):
            raise ValueError(f"unsupported image format: {image_format}")
        original = urlunsplit(parts._replace(query=urlencode({"format": image_format, "name": "orig"})))
        target = out_dir / f"{item['author']}-{item['id']}-photo-{Path(parts.path).stem}.{image_format}"
        print(f"  [图片 {index}/{len(item['images'])}] {target.name}", flush=True)
        if simulate:
            continue
        if not target.exists():
            temporary = target.with_suffix(target.suffix + ".part")
            try:
                with urlopen(Request(original, headers={"User-Agent": "Mozilla/5.0"}), timeout=60) as response:
                    if not response.headers.get_content_type().startswith("image/"):
                        raise ValueError(f"image request returned {response.headers.get_content_type()}")
                    with temporary.open("wb") as output:
                        shutil.copyfileobj(response, output)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        paths.append(str(target))
    return paths


def deliver_pending(state, dry_run):
    """Deliver new media files to NAS, including images added to an already delivered tweet."""
    pending = {}
    for key, entry in state.items():
        if entry.get("status") != "downloaded" or not entry.get("file"):
            continue
        paths = entry.get("files") or [entry["file"]]
        delivered = entry.get("delivered_files", paths if entry.get("delivered") else [])
        new_paths = [path for path in paths if path not in delivered and Path(path).exists()]
        if new_paths:
            pending[key] = (entry, new_paths)
    files = list(dict.fromkeys(path for _, paths in pending.values() for path in paths))
    if not pending:
        return 0, None
    if dry_run:
        print(f"[交付] 模拟运行：{len(files)} 个文件待交付，本次不复制", flush=True)
        return 0, None
    print(f"[交付] 正在将 {len(files)} 个图片/视频文件复制并校验到 NAS / X download……", flush=True)
    note = "X 收藏中的图片和视频；图片请求原图，视频最高 1080p。同一收藏的多个媒体均保留，正文不另存。"
    proc = subprocess.run(["nas-deliver", "--project", "X download", "--title", "书签图片与视频", "--note", note, "--",
                           *files],
                          capture_output=True, text=True, timeout=3600, env=run_env())
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        result = {}
    if result.get("status") != "delivered":
        return 0, (proc.stdout + proc.stderr).strip()[-300:] or f"nas-deliver exit {proc.returncode}"
    for entry, paths in pending.values():
        entry["delivered_files"] = list(dict.fromkeys([*entry.get("delivered_files", []), *paths]))
        entry["delivered"] = result.get("phone_folder") or now()
    save_state(state)
    print(f"[交付] 完成：{result.get('phone_folder') or 'X download'}", flush=True)
    return len(pending), None


def save_state(state):
    tmp = DATA / "state.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    os.replace(tmp, DATA / "state.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--out", type=Path, default=ROOT / "downloads")
    ap.add_argument("--skip-scrape", action="store_true", help="reuse data/bookmarks-latest.json")
    ap.add_argument("--dry-run", action="store_true", help="yt-dlp --simulate, no files, state not updated")
    args = ap.parse_args()

    if args.max <= 0:
        ap.error("--max must be greater than zero")
    with ExitStack() as resources:
        if not args.dry_run:
            DATA.mkdir(exist_ok=True)
            args.out.mkdir(parents=True, exist_ok=True)
            lock = resources.enter_context(open(DATA / "lock", "a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit("another run holds data/lock")

        snapshot = DATA / "bookmarks-latest.json"
        started = time.time()
        if args.skip_scrape:
            print("[采集] 使用上次采集清单", flush=True)
            items = json.loads(snapshot.read_text())["items"]
        elif args.dry_run:
            with tempfile.TemporaryDirectory(prefix="xdl-dry-run-") as temporary:
                items = scrape(args.max, Path(temporary) / "bookmarks.json")
        else:
            items = scrape(args.max, snapshot)

        state = json.loads((DATA / "state.json").read_text()) if (DATA / "state.json").exists() else {}
        counts = {"seen": len(items), "downloaded": 0, "no_video": 0, "failed": 0, "skipped": 0}
        seen_ids = {item["id"] for item in items}
        items += [{"id": tweet_id, "url": entry["url"], "author": entry["author"],
                   "hasVideo": entry.get("hasVideo", False), "images": entry.get("images", []),
                   "videos": entry.get("videos", [])}
                  for tweet_id, entry in state.items()
                  if entry["status"] == "failed" and tweet_id not in seen_ids]
        print(f"[处理] 共 {len(items)} 条收藏（包含范围外待重试记录）", flush=True)
        for index, item in enumerate(items, 1):
            entry = state.get(item["id"], {"attempts": 0, "first_seen": now()})
            images = item.get("images", [])
            processed = entry.get("status") in ("downloaded", "no_video")
            if processed and set(images).issubset(entry.get("downloaded_images", [])):
                counts["skipped"] += 1
                print(f"[处理 {index}/{len(items)}] @{item['author']}：已处理，跳过", flush=True)
                continue
            action = "模拟检查" if args.dry_run else "正在检查并下载图片和视频"
            print(f"[处理 {index}/{len(items)}] @{item['author']}：{action}……", flush=True)
            if processed:
                result = {key: entry[key] for key in ("status", "file", "files") if key in entry}
            elif images and not item["hasVideo"]:
                result = {"status": "no_video"}
            else:
                result = download(item, args.out, args.dry_run)
            if result["status"] == "no_video" and item["hasVideo"]:  # page showed a video: retry, don't settle
                result = {"status": "failed", "error": "yt-dlp found no video but the page shows one"}
            previous_files = entry.get("files") or ([entry["file"]] if entry.get("file") else [])
            files = list(dict.fromkeys([*previous_files, *(result.get("files") or
                                      ([result["file"]] if result.get("file") else []))]))
            try:
                files += [path for path in download_images(item, args.out, args.dry_run) if path not in files]
                result["downloaded_images"] = images
                if images and result["status"] == "no_video":
                    result["status"] = "downloaded"
            except (OSError, URLError, ValueError) as error:
                result.update(status="failed", error=f"image download failed: {error}")
            if files:
                result.update(file=files[-1], files=files)
            if entry.get("delivered") and "delivered_files" not in entry:
                result["delivered_files"] = previous_files
            counts[result["status"]] += 1
            print(f"[处理 {index}/{len(items)}] {item['id']} {item['author']}: "
                  f"{result['status']} {result.get('file') or result.get('error') or ''}", flush=True)
            if not args.dry_run:
                state[item["id"]] = {**entry, **result, "url": item["url"], "author": item["author"],
                                     "hasVideo": item["hasVideo"],
                                     "images": images,
                                     "videos": item.get("videos", entry.get("videos", [])),
                                     "attempts": entry["attempts"] + 1, "updated": now()}
                save_state(state)
            time.sleep(3)

        delivered, deliver_error = deliver_pending(state, args.dry_run)
        if deliver_error:
            print(f"NAS delivery failed, local files kept: {deliver_error}")
        if args.dry_run:
            print("[完成] 模拟运行结束，未下载或交付文件", flush=True)
        else:
            print(f"[完成] 新下载 {counts['downloaded']} 条，无可下载媒体 {counts['no_video']} 条，"
                  f"跳过 {counts['skipped']} 条，失败 {counts['failed']} 条，"
                  f"已交付 {delivered} 条", flush=True)
        summary = {"at": now(), "seconds": round(time.time() - started), **counts, "delivered": delivered,
                   "deliver_error": deliver_error, "dry_run": args.dry_run}
        if not args.dry_run:
            with open(DATA / "runs.log", "a") as log:
                log.write(json.dumps(summary) + "\n")
        print(json.dumps(summary))
        sys.exit(1 if counts["failed"] or deliver_error else 0)


if __name__ == "__main__":
    installed_python = ROOT / ".venv/bin/python"
    if installed_python.exists() and Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        os.execv(str(installed_python), [str(installed_python), "-I", str(Path(__file__).resolve()), *sys.argv[1:]])
    # -I omits the script directory; explicitly add this trusted project root.
    sys.path.insert(0, str(ROOT))
    from xdownload.cli import main as cli_main
    raise SystemExit(cli_main(["run", *sys.argv[1:]]))
