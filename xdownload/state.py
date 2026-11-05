import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent
ROOT = Path(os.environ.get("XDL_HOME", SOURCE if (SOURCE / "pyproject.toml").exists() else Path.home() / ".local/share/xdownload")).resolve()
DATA = ROOT / "data"
DEFAULTS = {"max": 40, "media": "both", "quality": "1080", "delivery": "local", "out": str(ROOT / "downloads")}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as output:
        json.dump(value, output, ensure_ascii=False, indent=1)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def settings():
    value = {**DEFAULTS, **read(DATA / "settings.json", {})}
    if not isinstance(value["max"], int) or value["max"] < 1:
        raise ValueError("收藏条数必须是正整数")
    if value["media"] not in ("both", "images", "videos") or value["quality"] not in ("1080", "720", "best"):
        raise ValueError("下载设置无效，请进入设置恢复默认")
    if value["delivery"] not in ("nas", "local"):
        raise ValueError("交付位置必须是 NAS 或仅本机")
    value["out"] = str(Path(value["out"]).expanduser().resolve())
    return value


def save_settings(value):
    atomic(DATA / "settings.json", value)


def fresh():
    return {"schema_version": 2, "entries": {}, "accounts": {}}


def load(migrate=False):
    path = DATA / "state.json"
    value = read(path, {})
    if "schema_version" in value:
        if value["schema_version"] != 2 or not isinstance(value.get("entries"), dict) or not isinstance(value.get("accounts"), dict):
            raise ValueError("state.json 格式或版本不支持；保留原文件，请检查备份")
        return value
    if not isinstance(value, dict) or any(not isinstance(entry, dict) or "status" not in entry for entry in value.values()):
        raise ValueError("旧 state.json 格式无效；未修改原文件")
    result = fresh()
    for key, old in value.items():
        entry = dict(old)
        paths = entry.get("files") or ([entry["file"]] if entry.get("file") else [])
        delivered = entry.get("delivered_files", paths if entry.get("delivered") else [])
        entry["files"] = list(dict.fromkeys(paths))
        entry["delivered_files"] = list(dict.fromkeys(delivered))
        entry.update(accounts=[], media={path: {"kind": "image" if "-photo-" in Path(path).name else "video",
            "downloaded": True, "delivered": path in delivered} for path in paths})
        entry["video_done"] = old["status"] in ("downloaded", "no_video") or any(record["kind"] == "video" for record in entry["media"].values())
        entry["legacy_unassigned"] = True
        result["entries"][key] = entry
    if migrate and path.exists():
        backup = DATA / "state.v1.backup.json"
        if not backup.exists():
            # Write and validate the backup before replacing any business state.
            atomic(backup, value)
        if read(backup) != value:
            raise ValueError("旧状态备份已存在且内容不同；未覆盖 state.json")
        json.loads(json.dumps(result))
        atomic(path, result)
    return result


def save(value):
    atomic(DATA / "state.json", value)


def history():
    directory = DATA / "runs"
    return [read(path) for path in sorted(directory.glob("*/result.json"), reverse=True)] if directory.exists() else []


def trim_runs():
    directories = sorted((DATA / "runs").glob("*/result.json"), reverse=True)
    for result in directories[30:]:
        directory = result.parent
        # Only files created by this application are eligible for rotation.
        for name in ("result.json", "events.jsonl", "run.log", "output.log"):
            (directory / name).unlink(missing_ok=True)
        if not any(directory.iterdir()):
            directory.rmdir()
