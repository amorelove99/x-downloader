import json
from urllib.error import URLError
from urllib.request import Request, urlopen
from . import state

DEFAULT_SERVER = "https://api.day.app"


def path():
    return state.DATA/"bark.json"


def config():
    value = state.read(path(), {}) or {}
    return value if value.get("key") else None


def save(key, server=None):
    state.atomic(path(), {"key": key, "server": (server or DEFAULT_SERVER).rstrip("/")})
    path().chmod(0o600)


def disable():
    path().unlink(missing_ok=True)


def send(title, body):
    """Push via Bark. Returns an error text, or None on success; never raises."""
    value = config()
    if not value:
        return "未配置 Bark"
    payload = json.dumps({"device_key": value["key"], "title": title, "body": body[:500], "group": "X Download"}).encode()
    request = Request(value.get("server", DEFAULT_SERVER).rstrip("/")+"/push", payload,
                      {"Content-Type": "application/json; charset=utf-8"})
    try:
        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read() or b"{}")
        return None if result.get("code", 200) == 200 else f"Bark 返回 {result.get('code')}：{result.get('message')}"
    except (URLError, OSError, ValueError) as error:
        return f"Bark 发送失败：{type(error).__name__}"


def failure(record):
    """Notify about a failed run; interrupts (130) are user actions and stay silent."""
    if not config():
        return
    counts = record.get("counts") or {}
    parts = [f"@{record['account']}"] if record.get("account") else []
    if counts.get("failed"):
        parts.append(f"失败 {counts['failed']} 条")
    reason = record.get("error") or record.get("delivery_error")
    if reason:
        parts.append(str(reason).strip().splitlines()[-1])
    parts.append(f"退出码 {record.get('exit_code')}")
    error = send("X 收藏下载失败", " · ".join(parts))
    if error:
        print(f"通知未发出：{error}")
