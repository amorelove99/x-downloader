import contextlib
import fcntl
import hashlib
import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from . import state

LABEL = "io.github.xdownloader.bookmarks"
CHILDREN = set()
LOCK = Path(tempfile.gettempdir()) / ("xdownload-" + hashlib.sha256(str(state.ROOT).encode()).hexdigest()[:20] + ".lock")


class BusyError(Exception):
    pass


def env():
    return {**os.environ, "XDL_HOME": str(state.ROOT), "PATH": os.pathsep.join([str(Path.home()/".local/bin"), "/opt/homebrew/bin", os.environ.get("PATH", "")])}


@contextmanager
def locked():
    with LOCK.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BusyError("已有任务运行，请查看进度或停止任务")
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def identity(pid):
    proc = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart=,command="], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def current():
    value = state.read(state.DATA / "current.json")
    if value and value.get("status") == "running" and identity(value["pid"]) != value["identity"]:
        value = {**value, "status": "interrupted", "message": "上次任务异常中断，可重新运行恢复"}
    return value


def active():
    value = current()
    return value if value and value["status"] == "running" else None


def terminate_children():
    for proc in list(CHILDREN):
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + 4
    for proc in list(CHILDREN):
        if proc.poll() is None:
            try:
                proc.wait(timeout=max(.01, deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
    CHILDREN.clear()


def popen(command, **kwargs):
    proc = subprocess.Popen(command, start_new_session=True, env=env(), **kwargs)
    CHILDREN.add(proc)
    value = state.read(state.DATA / "current.json")
    if value and value.get("pid") == os.getpid() and value.get("status") == "running":
        value.setdefault("children", []).append({"pid": proc.pid, "identity": identity(proc.pid)})
        state.atomic(state.DATA / "current.json", value)
    return proc


def call(command, timeout, input=None):
    proc = popen(command, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        output, errors = proc.communicate(input=input, timeout=timeout)
        return subprocess.CompletedProcess(command, proc.returncode, output, errors)
    except BaseException:
        terminate_children()
        raise
    finally:
        if proc.poll() is not None:
            CHILDREN.discard(proc)


def stop():
    value = active()
    if not value:
        print("当前没有运行中的任务")
        return 0
    # Recheck immediately before signaling; never trust a PID by itself.
    if identity(value["pid"]) != value["identity"]:
        raise ValueError("任务进程标识已变化，未发送停止信号")
    os.kill(value["pid"], signal.SIGTERM)
    for _ in range(80):
        if identity(value["pid"]) != value["identity"] or not active():
            print("任务已安全停止，已完成文件已保存")
            return 0
        time.sleep(.1)
    # Child processes run in separate sessions; validate their individual identities.
    latest = state.read(state.DATA / "current.json", value)
    for child in latest.get("children", []):
        if child["identity"] and identity(child["pid"]) == child["identity"] and os.getpgid(child["pid"]) == child["pid"]:
            os.killpg(child["pid"], signal.SIGTERM)
    time.sleep(.25)
    for child in latest.get("children", []):
        if child["identity"] and identity(child["pid"]) == child["identity"] and os.getpgid(child["pid"]) == child["pid"]:
            os.killpg(child["pid"], signal.SIGKILL)
    # Only background jobs own their whole process group.
    if value.get("own_session") and os.getpgid(value["pid"]) == value["pid"] and identity(value["pid"]) == value["identity"]:
        os.killpg(value["pid"], signal.SIGKILL)
        print("任务停止超时，已结束本任务进程组；下次运行恢复")
        return 130
    raise RuntimeError("任务尚未退出，请查看日志；未强杀共享终端进程组")


def task_output_path():
    directory = state.DATA / "task-output"
    directory.mkdir(parents=True, exist_ok=True)
    paths = sorted(directory.glob("*.log"))
    for path in paths[:-29]:
        path.unlink()
    return directory / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".log")


def scheduled(callback):
    path = task_output_path()
    with path.open("a", buffering=1) as output, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        return callback()


def background(arguments):
    if active():
        raise BusyError("已有任务运行")
    token = uuid.uuid4().hex
    state.DATA.mkdir(parents=True, exist_ok=True)
    log = task_output_path()
    with log.open("a") as output:
        proc = subprocess.Popen([sys.executable, "-m", "xdownload", *arguments, "--job-token", token],
                                cwd=state.ROOT, env=env(), stdin=subprocess.DEVNULL,
                                stdout=output, stderr=output, start_new_session=True)
    for _ in range(100):
        value = current()
        if value and value.get("job_token") == token:
            print(f"后台任务已启动：{value['run_id']}（PID {proc.pid}）")
            return 0
        if proc.poll() is not None:
            raise RuntimeError(f"后台任务未成功启动（退出 {proc.returncode}）；查看 {log}")
        time.sleep(.1)
    raise RuntimeError(f"后台任务尚未写出运行标识，请查看 {log}")


def schedule_path():
    return Path.home()/"Library/LaunchAgents"/(schedule_label()+".plist")


def schedule_label():
    # Read lazily so a broken schedule cannot prevent help, downloads or stopping.
    saved = state.read(state.DATA/"schedule.json", {}) or {}
    if not isinstance(saved, dict):
        raise ValueError("定时配置必须是 JSON 对象")
    label = saved.get("launchd_label", LABEL)
    if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", label):
        raise ValueError("定时任务标识无效")
    return label


def schedule_status():
    label = schedule_label()
    proc = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True)
    config = schedule_path()
    template = state.ROOT / "launchd" / (label + ".plist")
    selected = config if config.exists() else template
    interval = plistlib.loads(selected.read_bytes()).get("StartCalendarInterval", {}) if selected.exists() else {}
    days = (state.read(state.DATA/"schedule.json", {}) or {}).get("every_days", 1)
    return {"loaded": proc.returncode == 0, "days": days, "time": f"{interval.get('Hour',9):02d}:{interval.get('Minute',30):02d}",
            "timezone": time.tzname[0], "detail": proc.stdout.strip() if proc.returncode == 0 else proc.stderr.strip()}


def schedule(action, clock=None, days=None):
    domain = f"gui/{os.getuid()}"
    if action == "status":
        value = schedule_status()
        every = "每天" if value["days"] == 1 else f"每 {value['days']} 天"
        print(f"定时：{'已启用' if value['loaded'] else '已停用'} · {every} {value['time']} · 本机时区 {value['timezone']}")
        if value["loaded"]:
            relevant = [line.strip() for line in value["detail"].splitlines() if "last exit code" in line or "state =" in line]
            if relevant:
                print("最近状态：" + " · ".join(relevant))
        print(f"日志：{state.DATA / 'runs'}（启动错误见 task-output）；睡眠时不能执行，登出时不运行；锁屏运行未实测。")
        return 0
    if action == "disable":
        if schedule_status()["loaded"]:
            subprocess.run(["launchctl", "bootout", f"{domain}/{schedule_label()}"], check=True)
        print("每日定时已停用；配置与文件保留")
        return 0
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock or ""):
        raise ValueError("时间格式必须为 HH:MM，例如 09:30")
    saved = state.read(state.DATA/"schedule.json", {}) or {}
    days = days if days is not None else saved.get("every_days", 1)
    if not isinstance(days, int) or not 1 <= days <= 30:
        raise ValueError("间隔天数必须是 1 到 30 的整数")
    label = schedule_label()
    python = state.ROOT / ".venv/bin/python"
    if not python.exists():
        raise ValueError("请先双击启动器安装项目虚拟环境，再启用定时")
    hour, minute = map(int, clock.split(":"))
    config = {"Label": label, "ProgramArguments": [str(python), "-m", "xdownload", "run", "--scheduled"],
              "WorkingDirectory": str(state.ROOT), "EnvironmentVariables": {"PATH": env()["PATH"], "XDL_HOME": str(state.ROOT)},
              "StartCalendarInterval": {"Hour": hour, "Minute": minute},
              "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}
    if schedule_status()["loaded"]:
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], check=True)
    state.DATA.mkdir(parents=True, exist_ok=True)
    path = schedule_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = plistlib.loads(path.read_bytes())
        if existing.get("Label") != label:
            raise ValueError("现有定时文件不属于本项目，未覆盖")
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(plistlib.dumps(config))
    os.replace(temporary, path)
    subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
    state.atomic(state.DATA/"schedule.json", {**saved, "time": clock, "every_days": days})
    print(f"{'每天' if days == 1 else f'每 {days} 天'} {clock} 已启用，时区 {time.tzname[0]}")
    return 0
