import copy
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path
from . import jobs, media, notify, state
from .progress import Reporter


class ConditionsError(Exception):
    pass


def validate_snapshot(value, require_account=True):
    if not isinstance(value, dict) or value.get("status", "ok") != "ok" or not isinstance(value.get("items"), list):
        raise ConditionsError("采集失败或尚未登录 X，请在 ego lite 登录后重试")
    account = value.get("account")
    if require_account and (not isinstance(account, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", account)):
        raise ConditionsError("无法确认当前 X 账号；未使用上次账号代替，请检查 ego lite 登录状态")
    for item in value["items"]:
        if not isinstance(item, dict) or not re.fullmatch(r"\d+", str(item.get("id", ""))) or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", item.get("author", "")):
            raise ValueError("采集清单含无效收藏标识")
        if not re.fullmatch(r"https://x\.com/[A-Za-z0-9_]{1,15}/status/\d+", item.get("url", "")):
            raise ValueError("采集清单含非 X 收藏链接")
        if not isinstance(item.get("images", []), list):
            raise ValueError("采集清单图片列表格式错误")
        if not isinstance(item.get("videos", []), list):
            raise ValueError("采集清单视频列表格式错误")
    return value


def scrape(maximum, reporter, account_only=False, known=None):
    with tempfile.TemporaryDirectory(prefix="xdl-scrape-") as temporary:
        output = Path(temporary)/"bookmarks.json"
        progress = Path(temporary)/"progress.log"
        progress.touch()
        script_path = Path(__file__).parent/"scrape.mjs"
        script = (script_path.read_text().replace("__MAX__", str(maximum)).replace('"__OUT__"', json.dumps(str(output)))
                  .replace("__PROGRESS__", json.dumps(str(progress))).replace("__ACCOUNT_ONLY__", "true" if account_only else "false")
                  .replace("__KNOWN__", json.dumps(known)))
        reporter.emit("采集", f"正在打开 X 收藏页；本次上限 {maximum} 条")
        proc = jobs.popen(["ego-browser", "nodejs"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
        # File-backed stdout avoids pipe deadlock while the progress file is polled.
        import threading
        result = {}
        def communicate():
            result["output"] = proc.communicate(script)
        worker = threading.Thread(target=communicate, daemon=True)
        worker.start()
        started = time.monotonic()
        try:
            with progress.open() as updates:
                while worker.is_alive():
                    for line in updates.readlines():
                        reporter.emit("采集", f"{line.strip()} · 已用 {time.monotonic()-started:.0f}s")
                    if time.monotonic()-started > 300:
                        raise ConditionsError("采集超过 300 秒，请检查浏览器后重试")
                    time.sleep(.2)
                for line in updates.readlines():
                    reporter.emit("采集", line.strip())
            worker.join()
            stdout, stderr = result["output"]
            if proc.returncode != 0 or not output.exists():
                raise ConditionsError(f"浏览器采集未完成；可能需要登录或用户已接管。{(stdout+stderr)[-1000:]}")
            value = validate_snapshot(state.read(output))
            reporter.emit("账号", f"当前登录 @{value['account']}")
            return value
        except BaseException:
            jobs.terminate_children()
            raise
        finally:
            jobs.CHILDREN.discard(proc)


def nas_config():
    value = state.read(state.DATA/"nas.json")
    if value is None:
        value = state.read(Path.home()/".config/nas-delivery.json")
    if not value or not value.get("mount_root"):
        raise ValueError("NAS 未配置；可继续仅本机使用，或按 README 创建 data/nas.json")
    root = Path(value["mount_root"]).expanduser()
    folder = Path(value.get("delivery_folder", "交付"))
    if not root.is_absolute() or folder.is_absolute() or ".." in folder.parts:
        raise ValueError("mount_root 必须为绝对路径，delivery_folder 必须是共享内的相对路径")
    if not (root/folder/"X download").resolve().is_relative_to(root.resolve()):
        raise ValueError("交付目录不能指向共享之外")
    return value


def nas_mounted(config):
    root = Path(config["mount_root"]).expanduser().resolve()
    proc = subprocess.run(["/sbin/mount"], capture_output=True, text=True, check=True)
    return any(f" on {root} (smbfs" in line for line in proc.stdout.splitlines())


def snapshot_path(account):
    return state.DATA/"accounts"/account.lower()/"bookmarks.json"


def account_record(account):
    state.atomic(state.DATA/"account.json", {"account": account, "confirmed": state.now()})


def has_pending(entry, selection):
    images_done = set(entry.get("downloaded_images", []))
    return ((selection != "videos" and not set(entry.get("images", [])).issubset(images_done)) or
            (selection != "images" and not entry.get("video_done", False)) or bool(entry.get("missing_files")))


def known_ids(value, selection):
    """Per account: bookmarks fully handled; scrape stops after a run of them."""
    result = {}
    for key, entry in value["entries"].items():
        if (entry.get("status") in ("downloaded", "no_video") and not has_pending(entry, selection)
                and all(Path(path).exists() for path in entry.get("files", []))):
            for account in entry.get("accounts", []):
                if account not in entry.get("failures", {}):
                    result.setdefault(account, []).append(key)
    return result


def plan(snapshot, value, selection):
    rows = []
    for item in snapshot["items"]:
        entry = value["entries"].get(item["id"], {})
        missing = [path for path in entry.get("files", []) if not Path(path).exists()]
        item_entry = {**entry, "images": item.get("images", []), "missing_files": missing}
        status = "本地文件缺失" if missing else "待重试" if snapshot["account"].lower() in entry.get("failures", {}) else "待下载" if has_pending(item_entry, selection) else "已处理"
        pending = sum(not record.get("delivered") for path, record in entry.get("media", {}).items() if Path(path).exists())
        rows.append({"item": item, "status": status, "pending_delivery": pending, "missing": missing})
    return rows


def queue_items(snapshot, value, retry=None):
    items = [] if retry else list(snapshot["items"])
    present = {item["id"] for item in items}
    if retry != "delivery":
        for key, entry in value["entries"].items():
            if snapshot["account"].lower() in entry.get("failures", {}) and key not in present:
                items.append({"id": key, "url": entry["url"], "author": entry["author"],
                              "images": entry.get("images", []), "hasVideo": entry.get("hasVideo", False),
                              "videos": entry.get("videos", []), "time": entry.get("time")})
    return items


def preview(options):
    with jobs.locked():
        reporter = Reporter()
        try:
            value = state.load()
            snapshot = scrape(options["max"], reporter, known=known_ids(value, options["media"]))
            snapshot["items"] = queue_items(snapshot, value)
            snapshot["preview_queue"] = True
            rows = plan(snapshot, value, options["media"])
            for row in rows:
                reporter.emit("预览", f"{row['status']} · @{row['item']['author']} · {row['item']['url']} · 大小未知 · 待交付 {row['pending_delivery']} 文件")
                for path in row["missing"]:
                    reporter.emit("预览", f"本地缺失：{path}")
            state.atomic(state.DATA/"preview.json", {**snapshot, "created": state.now(), "options": options})
            reporter.emit("预览", "已保存专属预览；未下载媒体，未修改完成状态")
            return 0
        finally:
            reporter.close()


def add_file(entry, path, kind, source=None):
    entry.setdefault("media", {})[path] = {"kind": kind, "downloaded": True,
        "delivered": entry.get("media", {}).get(path, {}).get("delivered", False), "source": source}
    entry["files"] = list(dict.fromkeys([*entry.get("files", []), path]))
    entry["file"] = path


NAS_STALL_SECONDS = 120


class Stalled(Exception):
    pass


def sha256(path, tick=lambda: None):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024*1024), b""):
            digest.update(block)
            tick()
    return digest.hexdigest()


def copy_verified(source, folder, tick=lambda: None):
    """Copy into folder, verify sha256, publish atomically; identical existing file counts as delivered.
    tick() runs after every block (it may raise to abort), so a stalled NAS can be detected."""
    folder.mkdir(parents=True, exist_ok=True)
    tick()
    target = folder/source.name
    if target.is_symlink():
        raise ValueError(f"NAS 目标不能是符号链接：{target}")
    expected = sha256(source, tick)
    if target.exists():
        if sha256(target, tick) == expected:
            return target
        raise ValueError(f"NAS 已有同名但内容不同的文件：{target}")
    descriptor, name = tempfile.mkstemp(prefix=source.name+".", suffix=".part", dir=folder)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as writer, open(source, "rb") as reader:
            for block in iter(lambda: reader.read(1024*1024), b""):
                writer.write(block)
                tick()
        if sha256(temporary, tick) != expected:
            raise OSError("复制后校验不一致")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def copy_all(paths, project, stall=None):
    """Copy files in a worker thread; give up when the NAS makes no progress for `stall` seconds
    (e.g. an SMB mount frozen by a locked screen). Returns (done, errors, stalled)."""
    stall = NAS_STALL_SECONDS if stall is None else stall
    progress, cancelled = [time.monotonic()], threading.Event()
    done, errors, lock = {}, [], threading.Lock()

    def tick():
        if cancelled.is_set():
            raise Stalled()
        progress[0] = time.monotonic()

    def work():
        for path in paths:
            source = Path(path)
            month = time.strftime("%Y-%m", time.localtime(source.stat().st_mtime))
            try:
                folder = project/month
                if not folder.resolve().is_relative_to(project.resolve()):
                    raise ValueError("月份目录不能指向 NAS 项目之外")
                target = copy_verified(source, folder, tick)
                with lock:
                    done[path] = target
            except Stalled:
                return
            except (OSError, ValueError) as error:
                with lock:
                    errors.append(f"{source.name}: {error}")

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    while worker.is_alive() and time.monotonic()-progress[0] <= stall:
        worker.join(.5)
    stalled = worker.is_alive()
    if stalled:
        cancelled.set()
    with lock:
        return dict(done), list(errors), stalled


def deliveries(value, account, reporter, delivery="nas"):
    pending = {}
    missing = []
    for key, entry in sorted(value["entries"].items(), key=lambda kv: (kv[1].get("time") or "", kv[0]), reverse=True):
        if account.lower() not in entry.get("accounts", []):
            continue
        entry["missing_files"] = [path for path in entry.get("files", []) if not Path(path).exists()]
        missing += entry["missing_files"]
        for path, record in entry.get("media", {}).items():
            if not record.get("delivered") and Path(path).exists():
                pending.setdefault(path, []).append(entry)
    for path in missing:
        reporter.emit("错误", f"本地文件缺失：{path}；未计入本次成功")
    if not pending:
        return 0, "本地文件缺失" if missing else None
    if delivery == "local":
        reporter.emit("交付", f"当前为「仅本机」模式：{len(pending)} 个文件保留在本机，未交付 NAS；切回 NAS 后会补交")
        return 0, "本地文件缺失" if missing else None
    try:
        mounted = nas_mounted(nas_config())
    except (OSError, ValueError, subprocess.SubprocessError):
        mounted = False
    if not mounted:
        reporter.emit("交付", f"NAS 未挂载，已自动降级为仅本机保存：{len(pending)} 个文件待补交；挂载后选择补交")
        return 0, "本地文件缺失" if missing else None
    config = nas_config()
    project = Path(config["mount_root"]).expanduser()/config.get("delivery_folder", "交付")/"X download"
    reporter.emit("交付", f"NAS 复制与校验：{len(pending)} 个文件 → X download/按月份归档")
    started = time.monotonic()
    done, errors, stalled = copy_all(list(pending), project)
    months = {target.parent.name for target in done.values()}
    if stalled:
        reporter.emit("交付", f"NAS {NAS_STALL_SECONDS} 秒没有进展（常见于锁屏或 NAS 休眠），已放弃本次交付；"
                              f"{len(pending)-len(done)} 个文件保留在本机待补交，下次运行或选择补交会继续")
    for path, target in done.items():
        for entry in pending[path]:
            entry["media"][path]["delivered"] = True
            entry["media"][path]["delivery"] = str(target.parent)
            entry["delivered_files"] = list(dict.fromkeys([*entry.get("delivered_files", []), path]))
            entry["delivery_path"] = str(target.parent)
            entry["delivered"] = state.now()
            entry.pop("delivery_error", None)
    error = "；".join(errors)[-2000:] or None
    if error:
        for path, entries in pending.items():
            if path not in done:
                for entry in entries:
                    entry["delivery_error"] = error
        reporter.emit("错误", f"部分文件未交付，已保留本地；选择补交可重试。{error}")
    state.save(value)
    if done:
        folder = project/next(iter(months)) if len(months) == 1 else project
        state.atomic(state.DATA/"latest-delivery.json", {"status": "delivered", "nas_folder": str(folder), "months": sorted(months),
                     "files": [{"filename": target.name, "nas_path": str(target)} for target in done.values()]})
        reporter.emit("交付", f"已校验 {len(done)} 个文件 · {time.monotonic()-started:.1f}s · X download/{'、'.join(sorted(months))}")
    return len(done), error or ("本地文件缺失" if missing else None)


def run(options, retry=None):
    if options.get("background"):
        raise ValueError("后台运行须通过 CLI 调度")
    with jobs.locked():
        code = _run(options, retry)
    if code not in (0, 130) and not options.get("dry_run"):
        notify.failure(state.read(state.DATA/"current.json", {}) or {})
    return code


def _run(options, retry):
    dry = options.get("dry_run", False)
    run_id = time.strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:8]
    directory = None
    record = None
    if not dry:
        directory = state.DATA/"runs"/run_id
        directory.mkdir(parents=True)
        record = {"run_id": run_id, "pid": os.getpid(), "identity": jobs.identity(os.getpid()),
                  "own_session": os.getsid(0) == os.getpid(), "job_token": options.get("job_token"),
                  "started": state.now(), "updated": state.now(), "phase": "准备", "status": "running", "options": options}
        state.atomic(state.DATA/"current.json", record)
    reporter = Reporter(record, directory)
    started = time.monotonic()
    previous = signal.getsignal(signal.SIGTERM)
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    exit_code = 0
    counts = {"bookmarks": 0, "new": 0, "skipped": 0, "failed": 0, "images": 0, "videos": 0, "bytes": 0, "delivered": 0}
    try:
        value = state.load(migrate=not dry)
        known = {} if options.get("full") else known_ids(value, options["media"])
        if options.get("skip_scrape"):
            recent = state.read(state.DATA/"account.json")
            path = snapshot_path(recent["account"]) if recent else state.DATA/"bookmarks-latest.json"
            snapshot = validate_snapshot(state.read(path))
            reporter.emit("采集", f"使用 @{snapshot['account']} 上次已确认的清单，未打开浏览器")
        elif options.get("use_preview"):
            saved = state.read(state.DATA/"preview.json")
            if not saved:
                raise ValueError("没有预览清单，请先预览")
            current = scrape(1, reporter, account_only=True)
            if current["account"].lower() == saved["account"].lower():
                snapshot = validate_snapshot(saved)
                reporter.emit("预览", "账号一致，使用已确认的预览清单")
            else:
                reporter.emit("账号", "账号发生变化，重新采集当前账号收藏")
                snapshot = scrape(options["max"], reporter, known=known)
        else:
            snapshot = scrape(1 if retry else options["max"], reporter, account_only=bool(retry), known=known)
        account = snapshot["account"]
        account_key = account.lower()
        if not dry:
            account_record(account)
            value["accounts"][account_key] = {"confirmed": state.now()}
            if not retry:
                state.atomic(snapshot_path(account), snapshot)
                state.atomic(state.DATA/"bookmarks-latest.json", snapshot)
            record["account"] = account
        items = list(snapshot["items"]) if snapshot.get("preview_queue") and options.get("use_preview") else queue_items(snapshot, value, retry)
        counts["bookmarks"] = len(items)
        # New bookmarks get an increasing number (newer = larger) so file names sort by bookmark order.
        seqs = {}
        if not dry:
            for item in reversed(items):
                entry = value["entries"].get(item["id"], {})
                if "seq" not in entry and not entry.get("files"):
                    value["bookmark_seq"] = value.get("bookmark_seq", 0)+1
                    seqs[item["id"]] = value["bookmark_seq"]
        out = Path(options["out"])
        if not dry:
            out.mkdir(parents=True, exist_ok=True)
        for index, item in enumerate(items, 1):
            key = item["id"]
            entry = value["entries"].setdefault(key, {"attempts": 0, "first_seen": state.now(), "media": {}, "accounts": [], "files": [], "video_done": False})
            entry.update(url=item["url"], author=item["author"], images=item.get("images", []), hasVideo=item.get("hasVideo", False),
                         videos=item.get("videos", []), time=item.get("time") or entry.get("time"))
            entry["accounts"] = list(dict.fromkeys([*entry.get("accounts", []), account_key]))
            if key in seqs:
                entry["seq"] = seqs[key]
            item["seq"] = entry.get("seq")
            entry["missing_files"] = [path for path in entry.get("files", []) if not Path(path).exists()]
            reporter.emit("处理", f"{index}/{len(items)} 收藏 · @{item['author']} · 已完成媒体 {counts['images']+counts['videos']}", item_index=index, item_total=len(items))
            if dry:
                reporter.emit("模拟", f"{item['url']} · {'待处理' if has_pending(entry, options['media']) else '已处理'} · 大小未知")
                continue
            if not has_pending(entry, options["media"]):
                counts["skipped"] += 1
                if not has_pending(entry, "both"):
                    entry.setdefault("failures", {}).pop(account_key, None)
                state.save(value)
                continue
            previous_failure = entry.get("failures", {}).get(account_key)
            entry["attempts"] = entry.get("attempts", 0)+1
            entry["status"] = "failed"
            entry.setdefault("failures", {})[account_key] = {"at": state.now(), "error": "任务尚未完成，可重试", "attempts": entry["attempts"]}
            state.save(value)
            errors = []
            if options["media"] != "videos":
                for source in item.get("images", []):
                    target, _ = media.image_target(item, source, out)
                    if source in entry.get("downloaded_images", []) and target.exists():
                        continue
                    try:
                        path = media.image(item, source, out, reporter)
                        add_file(entry, path, "image", source)
                        entry["downloaded_images"] = list(dict.fromkeys([*entry.get("downloaded_images", []), source]))
                        counts["images"] += 1
                        counts["bytes"] += Path(path).stat().st_size
                        state.save(value)
                    except (OSError, ValueError) as error:
                        errors.append(f"图片 {source}: {error}")
                        reporter.emit("错误", f"{item['url']} · {errors[-1]} · 第 {entry['attempts']} 次")
            missing_videos = [path for path in entry["missing_files"] if entry.get("media", {}).get(path, {}).get("kind") == "video"]
            if options["media"] != "images" and (not entry.get("video_done") or missing_videos):
                try:
                    # Image-only posts need no yt-dlp request; unknown old posts still get checked.
                    def completed(path):
                        if not Path(path).is_file():
                            raise OSError(f"视频输出文件不存在：{path}")
                        add_file(entry, path, "video", item["url"])
                        counts["videos"] += 1
                        counts["bytes"] += Path(path).stat().st_size
                        state.save(value)
                    if not ((item.get("images") and not item.get("hasVideo")) or ("videos" in item and not item.get("hasVideo"))):
                        media.video(item, out, state.DATA/"archive.txt", options["quality"], reporter,
                                    completed=completed, repair=bool(missing_videos),
                                    known_files=[path for path, record in entry["media"].items()
                                                 if record["kind"] == "video"])
                    entry["video_done"] = True
                    state.save(value)
                except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                    errors.append(f"视频: {error}")
                    reporter.emit("错误", f"{item['url']} · {errors[-1]} · 第 {entry['attempts']} 次")
            entry["missing_files"] = [path for path in entry.get("files", []) if not Path(path).exists()]
            if entry["missing_files"]:
                errors.append("本地文件缺失："+", ".join(entry["missing_files"]))
            entry["status"] = "failed" if errors else "downloaded" if entry.get("files") else "no_video"
            entry["updated"] = state.now()
            if errors:
                counts["failed"] += 1
                entry["error"] = "\n".join(errors)
                entry.setdefault("failures", {})[account_key] = {"at": state.now(), "error": entry["error"], "attempts": entry["attempts"]}
            else:
                counts["new"] += 1
                if previous_failure and has_pending(entry, "both"):
                    entry["failures"][account_key] = previous_failure
                    entry["error"] = previous_failure["error"]
                else:
                    entry.setdefault("failures", {}).pop(account_key, None)
                    entry.pop("error", None)
            state.save(value)
        delivery_error = None
        if not dry and retry != "download":
            counts["delivered"], delivery_error = deliveries(value, account, reporter, "nas" if retry == "delivery" else options.get("delivery", "local"))
        exit_code = 1 if counts["failed"] or delivery_error else 0
        if record is not None:
            record["delivery_error"] = delivery_error
        reporter.emit("完成", f"{'模拟运行；未写入业务状态或下载文件' if dry else '任务完成'} · 用时 {time.monotonic()-started:.1f}s · 收藏 {counts['bookmarks']} · 新增 {counts['new']} · 跳过 {counts['skipped']} · 失败 {counts['failed']} · 图片 {counts['images']} · 视频 {counts['videos']} · {counts['bytes']/1024/1024:.1f} MB · NAS 已交付 {counts['delivered']} 文件")
    except KeyboardInterrupt:
        exit_code = 130
        reporter.emit("停止", "任务已中断，已完成文件与记录保留；下次运行继续")
    except ConditionsError as error:
        exit_code = 4
        reporter.emit("错误", str(error))
        if record is not None:
            record["error"] = str(error)
        if directory:
            with (directory/"run.log").open("a") as output:
                output.write(traceback.format_exc())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        exit_code = 2 if isinstance(error, ValueError) else 4
        reporter.emit("错误", str(error))
        if record is not None:
            record["error"] = str(error)
        if directory:
            with (directory/"run.log").open("a") as output:
                output.write(traceback.format_exc())
    except BaseException:
        exit_code = 4
        if directory:
            with (directory/"run.log").open("a") as output:
                output.write(traceback.format_exc())
        raise
    finally:
        jobs.terminate_children()
        signal.signal(signal.SIGTERM, previous)
        if record is not None:
            record.update(status="interrupted" if exit_code == 130 else "success" if exit_code == 0 else "failed",
                          exit_code=exit_code, ended=state.now(), seconds=round(time.monotonic()-started, 1), counts=counts)
            state.atomic(directory/"result.json", record)
            state.atomic(state.DATA/"current.json", record)
            with (state.DATA/"runs.log").open("a") as output:
                output.write(json.dumps(record, ensure_ascii=False)+"\n")
            state.trim_runs()
        reporter.close()
    return exit_code
