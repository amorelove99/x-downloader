import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from . import jobs


def seq_prefix(item):
    """Bookmark order prefix for newly downloaded files; older files keep their names."""
    return f"{item['seq']:06d}_" if item.get("seq") else ""


def image_target(item, source, out):
    parts = urlsplit(source)
    if parts.scheme != "https" or parts.netloc != "pbs.twimg.com" or not parts.path.startswith("/media/"):
        raise ValueError(f"非 X 原图地址：{source}")
    extension = parse_qs(parts.query).get("format", [Path(parts.path).suffix.lstrip(".") or "jpg"])[0]
    if extension not in ("jpg", "jpeg", "png", "webp", "gif"):
        raise ValueError(f"不支持的图片格式：{extension}")
    for value in (item["id"], item["author"], Path(parts.path).stem):
        if not value or any(char in value for char in "/\\\x00") or value in (".", ".."):
            raise ValueError("媒体文件名包含非法路径字符")
    target = out / f"{seq_prefix(item)}{item['author']}-{item['id']}-photo-{Path(parts.path).stem}.{extension}"
    original = urlunsplit(parts._replace(query=urlencode({"format": extension, "name": "orig"})))
    return target, original


def image(item, source, out, reporter):
    target, original = image_target(item, source, out)
    if target.exists():
        reporter.emit("下载", f"复用本地图片 {target.name}")
        return str(target)
    temporary = target.with_suffix(target.suffix + ".part")
    started = time.monotonic()
    count = 0
    try:
        with urlopen(Request(original, headers={"User-Agent": "Mozilla/5.0"}), timeout=60) as response:
            if not response.headers.get_content_type().startswith("image/"):
                raise ValueError(f"图片请求返回 {response.headers.get_content_type()}")
            total = int(response.headers["Content-Length"]) if response.headers.get("Content-Length") else None
            with temporary.open("wb") as output:
                while True:
                    block = response.read(128*1024)
                    if not block:
                        break
                    output.write(block)
                    count += len(block)
                    speed = count / max(.001, time.monotonic()-started)
                    percent = f" · {count/total:.0%}" if total else " · 总大小未知"
                    eta = f" · 剩余 {(total-count)/speed:.0f}s" if total else ""
                    reporter.emit("下载", f"{target.name} · {count:,} 字节{percent} · {speed/1024:.1f} KB/s{eta}",
                                  transient=True, downloaded_bytes=count, total_bytes=total, speed=speed, file_key=str(target))
            if total is not None and count != total:
                raise OSError(f"图片不完整：收到 {count} 字节，预期 {total}")
        os.replace(temporary, target)
        reporter.emit("下载", f"图片完成 {target.name} · {count:,} 字节")
        return str(target)
    finally:
        temporary.unlink(missing_ok=True)


def parse_video_line(line):
    if line.startswith("XDL_PROGRESS:"):
        return "progress", json.loads(line[len("XDL_PROGRESS:"):])
    if line.startswith("XDL_FILE:"):
        value = json.loads(line[len("XDL_FILE:"):])
        if not isinstance(value, str):
            raise ValueError("yt-dlp 文件路径输出格式错误")
        return "file", value
    return "message", line


def select_video_urls(item, quality):
    videos = item.get("videos") or []
    urls = []
    limit = None if quality == "best" else int(quality)
    for vid in videos:
        variants = [v for v in (vid.get("variants") or []) if isinstance(v, dict) and v.get("url")]
        if not variants:
            if vid.get("url"):
                urls.append(vid["url"])
            continue
        ordered = sorted(variants, key=lambda v: v.get("bitrate") or 0, reverse=True)
        if limit is not None:
            matched = [v for v in ordered if v.get("height") and min(v.get("width") or v["height"], v["height"]) <= limit]
            chosen = matched[0] if matched else ordered[-1]
        else:
            chosen = ordered[0]
        urls.append(chosen["url"])
    return urls


def video(item, out, archive, quality, reporter, completed=None, repair=False, known_files=()):
    direct_urls = select_video_urls(item, quality)
    prefix, kind = seq_prefix(item), "video-" if item.get("seq") else ""
    output_template = (str(out / f"{prefix}{item['author']}-{item['id']}-{kind}%(id)s.%(ext)s") if direct_urls
                       else str(out / f"{prefix}%(uploader_id)s-%(id)s{'-video' if kind else ''}.%(ext)s"))
    command = ["yt-dlp", "--ignore-config", "--no-playlist", "--newline", "--progress", "--no-colors",
               "--retries", "3", "--socket-timeout", "30", "--download-archive", str(archive),
               "--progress-template", 'download:XDL_PROGRESS:%(progress)j',
               "--print", 'after_move:XDL_FILE:%(filepath)j',
               "-o", output_template]
    if quality != "best" and not direct_urls:
        command += ["-S", "res:"+quality]
    if repair:
        command += ["--no-download-archive"]
    command.extend(direct_urls or [item["url"]])
    proc = jobs.popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      text=True, bufsize=1)
    lines = queue.Queue()
    def reader():
        try:
            for line in proc.stdout:
                lines.put(line.rstrip())
        finally:
            lines.put(None)
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    started = time.monotonic()
    files, errors = [], []
    try:
        while True:
            if time.monotonic()-started > 900:
                raise subprocess.TimeoutExpired(command, 900)
            try:
                line = lines.get(timeout=.25)
            except queue.Empty:
                continue
            if line is None:
                break
            kind, value = parse_video_line(line)
            if kind == "file":
                files.append(value)
                if completed is not None:
                    completed(value)
                reporter.emit("下载", f"视频完成 {Path(value).name}")
            elif kind == "progress":
                downloaded = value.get("downloaded_bytes") or 0
                total = value.get("total_bytes") or value.get("total_bytes_estimate")
                estimated = "（估计）" if value.get("total_bytes_estimate") and not value.get("total_bytes") else ""
                speed = value.get("speed")
                fraction = f" · {downloaded/total:.0%}{estimated}" if total else " · 总大小未知"
                rate = f" · {speed/1024:.1f} KB/s" if speed else ""
                eta = f" · 剩余 {value['eta']}s" if value.get("eta") is not None else ""
                reporter.emit("下载", f"@{item['author']} · {downloaded:,} 字节{fraction}{rate}{eta}", transient=True,
                              downloaded_bytes=downloaded, total_bytes=total, speed=speed, file_key=value.get("filename"))
            elif line:
                errors.append(line)
                errors = errors[-30:]
                phase = "合并/转码" if line.startswith(("[Merger]", "[Video", "[Fixup")) else "视频"
                reporter.emit(phase, line, speed=None)
        proc.wait(timeout=5)
        if proc.returncode:
            text = "\n".join(errors)
            if "No video could be found in this tweet" in text or "Unsupported URL" in text:
                if not item.get("hasVideo"):
                    return []
            raise RuntimeError(text or f"yt-dlp 退出 {proc.returncode}")
        if not files and any("already been recorded in the archive" in line for line in errors):
            if not known_files or not all(Path(path).is_file() for path in known_files):
                raise RuntimeError("archive 已有记录但状态中缺少本地文件；请检查历史文件，未静默标记成功")
        return list(dict.fromkeys(files))
    except BaseException:
        jobs.terminate_children()
        raise
    finally:
        jobs.CHILDREN.discard(proc)
        proc.stdout.close()
        thread.join(timeout=1)
