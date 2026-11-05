import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from . import jobs, notify, runner, state


def options(args):
    result = state.settings()
    for key in ("max", "out", "media", "quality", "delivery"):
        value = getattr(args, key, None)
        if value is not None:
            result[key] = str(value) if key == "out" else value
    if result["max"] < 1:
        raise ValueError("收藏条数必须大于零")
    result["out"] = str(Path(result["out"]).expanduser().resolve())
    for key in ("dry_run", "skip_scrape", "use_preview", "job_token"):
        result[key] = getattr(args, key, False)
    return result


def parser():
    ap = argparse.ArgumentParser(prog="xdl", description="X 收藏图片与视频下载；双击启动器或运行 xdl menu 打开中文菜单")
    sub = ap.add_subparsers(dest="command")
    sub.add_parser("menu", help="中文数字菜单")
    run = sub.add_parser("run", help="下载新增收藏")
    run.add_argument("--max", type=int)
    run.add_argument("--out", type=Path)
    run.add_argument("--media", choices=("both", "images", "videos"))
    run.add_argument("--quality", choices=("1080", "720", "best"))
    run.add_argument("--delivery", choices=("nas", "local"), help="nas：交付 NAS（未挂载时自动仅留本机）；local：仅本机")
    for name in ("background", "dry-run", "skip-scrape"):
        run.add_argument("--"+name, action="store_true")
    run.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--use-preview", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--job-token", help=argparse.SUPPRESS)
    preview = sub.add_parser("preview", help="采集预览，不下载媒体")
    preview.add_argument("--max", type=int)
    status = sub.add_parser("status", help="当前任务状态")
    status.add_argument("--watch", action="store_true")
    sub.add_parser("history", help="最近运行记录")
    logs = sub.add_parser("logs", help="任务日志")
    logs.add_argument("--run")
    logs.add_argument("--follow", action="store_true")
    retry = sub.add_parser("retry", help="重试当前账号失败队列")
    exclusive = retry.add_mutually_exclusive_group()
    exclusive.add_argument("--download-only", action="store_true")
    exclusive.add_argument("--delivery-only", action="store_true")
    retry.add_argument("--job-token", help=argparse.SUPPRESS)
    sub.add_parser("stop", help="安全停止当前任务")
    schedule = sub.add_parser("schedule", help="每日定时管理")
    schedule_sub = schedule.add_subparsers(dest="action", required=True)
    schedule_sub.add_parser("status")
    enable = schedule_sub.add_parser("enable")
    enable.add_argument("--time", required=True)
    enable.add_argument("--every-days", type=int, help="每隔几天运行一次，默认沿用已保存值")
    schedule_sub.add_parser("disable")
    bark = sub.add_parser("notify", help="Bark 失败通知")
    bark_sub = bark.add_subparsers(dest="action", required=True)
    bark_set = bark_sub.add_parser("set")
    bark_set.add_argument("--key", required=True, help="Bark 设备 Key")
    bark_set.add_argument("--server", help="自建 Bark 服务器地址，默认 https://api.day.app")
    bark_sub.add_parser("test")
    bark_sub.add_parser("off")
    doctor = sub.add_parser("doctor", help="环境检查")
    doctor.add_argument("--login", action="store_true", help="打开浏览器并识别登录账号")
    opening = sub.add_parser("open", help="打开文件夹")
    opening.add_argument("target", nargs="?", choices=("nas", "local", "latest"), default="local")
    return ap


def show_status(watch=False):
    from .progress import Reporter
    reporter = Reporter() if watch else None
    last = None
    try:
        while True:
            value = jobs.current()
            if not value:
                print("暂无运行记录")
                return 0
            signature = (value.get("updated"), value.get("message"), value["status"])
            if signature != last:
                message = f"{value['run_id']} · {value['status']} · {value.get('message', '')}"
                if reporter:
                    fields = {key:value[key] for key in ("item_index","item_total","downloaded_bytes","total_bytes","speed","file_key") if key in value}
                    reporter.emit(value.get("phase", "任务"), message, transient=value["status"] == "running", **fields)
                else:
                    print(message)
                    if value.get("counts"):
                        print(json.dumps(value["counts"], ensure_ascii=False))
                last = signature
            if not watch or value["status"] != "running":
                return 0
            time.sleep(.25)
    finally:
        if reporter:
            reporter.close()


def show_history():
    for row in state.history():
        counts = row.get("counts", {})
        print(f"{row['run_id']} · {row['status']} · @{row.get('account','未确认')} · 图片 {counts.get('images',0)} · 视频 {counts.get('videos',0)} · 失败 {counts.get('failed',0)} · {row.get('seconds',0)}s")
    return 0


def show_logs(run_id=None, follow=False):
    current = jobs.current()
    if run_id is None:
        if not current:
            print("暂无运行日志")
            return 0
        run_id = current["run_id"]
    if not run_id or Path(run_id).name != run_id or run_id in (".", ".."):
        raise ValueError("运行编号无效")
    path = state.DATA/"runs"/run_id/"run.log"
    if not path.exists():
        raise ValueError("该次运行日志不存在")
    with path.open() as stream:
        while True:
            print(stream.read(), end="", flush=True)
            current = jobs.current()
            if not follow or not current or current["run_id"] != run_id or current["status"] != "running":
                if follow and current and current["run_id"] == run_id:
                    print(f"任务结束：{current['status']} · 退出码 {current.get('exit_code', '未知')}")
                return 0
            time.sleep(.5)


def open_folder(target):
    if target == "local":
        path = Path(state.settings()["out"])
    else:
        config = runner.nas_config()
        if not runner.nas_mounted(config):
            raise runner.ConditionsError("NAS 尚未挂载，请挂载后再打开")
        project = Path(config["mount_root"]).expanduser()/config.get("delivery_folder", "交付")/"X download"
        if target == "latest":
            result = state.read(state.DATA/"latest-delivery.json")
            if not result:
                raise ValueError("暂无本程序新版记录的交付批次")
            # Only open a recorded batch within the configured project folder.
            path = Path(result["nas_folder"]) if result.get("nas_folder") else None
            if path is None:
                raise ValueError("本次交付工具未返回本机批次路径，请从 NAS 项目打开")
            if not path.resolve().is_relative_to(project.resolve()):
                raise ValueError("交付路径不属于 X download 项目")
        else:
            path = project
    if not path.is_dir():
        raise ValueError(f"文件夹尚不存在：{path}")
    subprocess.run(["open", str(path)], check=True)
    return 0


def doctor(login=False):
    errors = 0
    print(f"Python：{sys.version.split()[0]} · {sys.executable}")
    for name in ("ego-browser", "yt-dlp", "ffmpeg"):
        path = shutil.which(name, path=jobs.env()["PATH"])
        print(f"{'正常' if path else '缺失'}：{name} · {path or '请安装此外部工具后重新检查；启动器不会自动安装系统工具'}")
        errors += not bool(path)
    available = importlib.util.find_spec("rich") is not None
    print(f"Rich：{'已安装' if available else '未安装；请双击启动器安装项目依赖'}")
    errors += not available
    config = state.settings()
    value = state.load()
    for path in (state.DATA, Path(config["out"])):
        existing = path
        while not existing.exists():
            existing = existing.parent
        writable = os.access(existing, os.W_OK)
        free = shutil.disk_usage(existing).free/1024**3
        print(f"目录 {path} · {'可写' if writable else '不可写'} · 可用 {free:.1f} GB")
        errors += not writable
    if config["delivery"] == "nas":
        try:
            nas = runner.nas_config()
            mounted = runner.nas_mounted(nas)
            print(f"NAS：{'已挂载 SMB' if mounted else '尚未挂载；本地文件保留，挂载后补交'} · {nas['mount_root']}")
            errors += not mounted
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"NAS：{error}")
            errors += 1
    else:
        print("NAS：未启用，文件仅保存到本地")
    print(f"状态版本：{value['schema_version']} · 收藏记录 {len(value['entries'])}")
    try:
        with jobs.locked():
            print("运行锁：空闲")
    except jobs.BusyError:
        print("运行锁：任务占用")
    jobs.schedule("status")
    if login:
        from .progress import Reporter
        with jobs.locked():
            reporter = Reporter()
            try:
                snapshot = runner.scrape(1, reporter, account_only=True)
                runner.account_record(snapshot["account"])
            finally:
                reporter.close()
    return 4 if errors else 0


def choose(prompt, choices, default=None):
    while True:
        value = input(prompt).strip()
        if not value and default is not None:
            return default
        if value in choices:
            return value
        print("输入无效，请输入菜单中的数字")


def positive(prompt):
    while True:
        value = input(prompt).strip()
        if value == "0":
            return None
        if value.isdigit() and int(value)>0:
            return int(value)
        print("请输入正整数，0 返回")


def fail_list():
    account = state.read(state.DATA/"account.json")
    if not account:
        print("尚未确认账号")
        return
    found = False
    for entry in state.load()["entries"].values():
        if account["account"].lower() in entry.get("accounts", []) and (entry.get("failures", {}).get(account["account"].lower()) or entry.get("delivery_error") or entry.get("missing_files")):
            found = True
            print(f"{entry['url']} · {entry.get('error') or entry.get('delivery_error') or entry.get('missing_files')}")
    if not found:
        print("当前账号暂无失败记录")


def result_page(code):
    print(f"任务退出码：{code}。0 成功，1 部分失败，4 条件不满足，130 已停止")
    target = "nas" if state.settings()["delivery"] == "nas" else "local"
    while True:
        choice = choose(f"1 打开{' NAS' if target == 'nas' else '本地下载'}  2 查看失败  0 返回菜单：", {"0", "1", "2"})
        if choice == "0":
            return
        if choice == "1":
            open_folder(target)
        else:
            fail_list()


def menu_run(**updates):
    if jobs.active():
        choice = choose("已有任务：1 查看运行进度  2 停止任务  0 返回：", {"0", "1", "2"})
        if choice == "1":
            show_status(True)
        elif choice == "2":
            jobs.stop()
        return
    config = state.settings()
    config.update(updates)
    result_page(runner.run(config))


def settings_menu():
    while True:
        config = state.settings()
        media_name = {"both":"图片与视频", "images":"仅图片", "videos":"仅视频"}[config["media"]]
        quality_name = {"1080":"最高1080p", "720":"最高720p", "best":"最佳可用"}[config["quality"]]
        delivery_name = {"nas":"交付 NAS（未挂载时自动仅留本机）", "local":"仅本机"}[config["delivery"]]
        print(f"当前：{config['max']} 条 · {media_name} · {quality_name} · {config['out']} · {delivery_name}")
        choice = choose("1 收藏条数  2 媒体  3 视频质量  4 本地目录  5 恢复默认  6 创建快捷入口  7 交付位置  8 Bark 通知  0 返回：", set("012345678"))
        if choice == "0":
            return
        if choice == "1":
            number = positive("收藏条数（0 返回）：")
            if number is not None:
                config["max"] = number
        elif choice == "2":
            selected = choose("1 图片与视频  2 仅图片  3 仅视频  0 返回：", set("0123"))
            if selected != "0":
                config["media"] = {"1":"both", "2":"images", "3":"videos"}[selected]
        elif choice == "3":
            selected = choose("1 最高1080p  2 最高720p  3 最佳可用  0 返回：", set("0123"))
            if selected != "0":
                config["quality"] = {"1":"1080", "2":"720", "3":"best"}[selected]
        elif choice == "4":
            path = input("本地目录绝对路径（0 返回）：").strip()
            if path != "0":
                target = Path(path).expanduser()
                if not target.is_absolute():
                    raise ValueError("请输入绝对路径")
                config["out"] = str(target.resolve())
        elif choice == "5":
            config = dict(state.DEFAULTS)
        elif choice == "6":
            create_shortcut()
        elif choice == "8":
            print("Bark 通知：" + ("已开启（任务失败时推送）" if notify.config() else "未开启"))
            selected = choose("1 设置设备 Key  2 发送测试  3 关闭  0 返回：", set("0123"))
            if selected == "1":
                key = input("Bark 设备 Key（0 返回）：").strip()
                if key != "0" and key:
                    notify_action("set", key)
            elif selected == "2":
                notify_action("test")
            elif selected == "3":
                notify_action("off")
        elif choice == "7":
            selected = choose("1 交付 NAS（未挂载时自动仅留本机）  2 仅本机  0 返回：", set("012"))
            if selected != "0":
                config["delivery"] = {"1":"nas", "2":"local"}[selected]
        state.save_settings(config)


def create_shortcut():
    selected = choose("1 创建桌面启动入口  2 创建 ~/.local/bin/xdl  0 返回：", set("012"))
    if selected == "0":
        return
    if selected == "1":
        target = Path.home()/"Desktop/启动 X Download.command"
        source = state.ROOT/"启动 X Download.command"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            if target.is_symlink() and target.resolve() == source:
                print("桌面入口已存在")
                return
            raise ValueError("桌面同名文件已存在，未覆盖")
        target.symlink_to(source)
    else:
        target = Path.home()/".local/bin/xdl"
        python = state.ROOT/".venv/bin/python"
        if not python.exists():
            raise ValueError("请先通过双击启动器安装虚拟环境")
        import shlex
        contents = '#!/bin/sh\n# X Download launcher: '+str(state.ROOT)+'\nexport XDL_HOME='+shlex.quote(str(state.ROOT))+'\nexec '+shlex.quote(str(python))+' -m xdownload "$@"\n'
        if target.exists() or target.is_symlink():
            if target.is_file() and target.read_text() == contents:
                print("命令入口已存在")
                return
            raise ValueError("~/.local/bin/xdl 已存在，未覆盖同名程序")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
        target.chmod(0o755)
    print(f"已创建：{target}")


def menu():
    if not sys.stdin.isatty():
        parser().print_help()
        return 0
    while True:
        account = state.read(state.DATA/"account.json")
        runs = state.history()
        print("\nX Download · 图片与视频收藏下载")
        print(f"账号：@{account['account']}（上次确认 {account['confirmed']}；运行时重新检查）" if account else "账号：运行时识别")
        print("目标：NAS / X download")
        if runs:
            row = runs[0]
            counts = row.get("counts", {})
            print(f"上次：{row['status']} · 图片 {counts.get('images',0)} · 视频 {counts.get('videos',0)} · 失败 {counts.get('failed',0)}")
        print("\n1 开始下载新增收藏\n2 采集更多历史收藏\n3 预览本次下载清单\n4 重试失败 / 补交到 NAS\n5 查看任务、进度和日志\n6 打开下载文件夹\n7 每日定时设置\n8 下载设置\n9 检查运行环境\n0 退出")
        try:
            choice = choose("输入数字，回车确认 [默认 1]：", set("0123456789"), "1")
            if choice == "0":
                return 0
            if choice == "1":
                menu_run()
            elif choice == "2":
                selected = choose("1 最近100条  2 最近500条  3 自定义  0 返回：", set("0123"))
                maximum = {"1":100, "2":500}.get(selected)
                if selected == "3":
                    maximum = positive("收藏条数（0 返回）：")
                if maximum:
                    menu_run(max=maximum, full=True)
            elif choice == "3":
                if jobs.active():
                    menu_run()
                    continue
                runner.preview(state.settings())
                while True:
                    selected = choose("1 查看清单  2 确认开始  0 返回：", set("012"))
                    if selected == "0":
                        break
                    if selected == "1":
                        saved = state.read(state.DATA/"preview.json")
                        for row in runner.plan(saved, state.load(), state.settings()["media"]):
                            print(f"{row['status']} · {row['item']['url']} · 大小未知")
                    else:
                        saved = state.read(state.DATA/"preview.json")
                        menu_run(**saved["options"], use_preview=True)
                        break
            elif choice == "4":
                selected = choose("1 重试下载失败  2 仅补交 NAS  3 全部重试  0 返回：", set("0123"))
                if selected != "0":
                    result_page(runner.run(state.settings(), {"1":"download", "2":"delivery", "3":"all"}[selected]))
            elif choice == "5":
                while True:
                    selected = choose("1 当前任务实时进度  2 最近运行  3 某次日志  4 后台启动  5 停止当前任务  0 返回：", set("012345"))
                    if selected == "0":
                        break
                    if selected == "1":
                        show_status(True)
                    elif selected == "2":
                        show_history()
                    elif selected == "3":
                        rows = state.history()
                        for number, row in enumerate(rows, 1):
                            print(f"{number} {row['started']} · {row['status']} · @{row.get('account','未确认')}")
                        if rows:
                            selected_log = choose("输入日志序号（0 返回）：", {str(i) for i in range(len(rows)+1)})
                            if selected_log != "0":
                                show_logs(rows[int(selected_log)-1]["run_id"])
                        else:
                            print("暂无运行日志")
                    elif selected == "4":
                        jobs.background(["run"])
                    elif selected == "5":
                        jobs.stop()
            elif choice == "6":
                selected = choose("1 NAS项目  2 最近批次  3 本地下载  0 返回：", set("0123"))
                if selected != "0":
                    open_folder({"1":"nas", "2":"latest", "3":"local"}[selected])
            elif choice == "7":
                while True:
                    jobs.schedule("status")
                    selected = choose("1 设置时间  2 启用  3 停用  0 返回：", set("0123"))
                    if selected == "0":
                        break
                    if selected == "1":
                        value = input("运行时间 HH:MM（0 返回）：").strip()
                        if value != "0":
                            import re
                            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                                raise ValueError("时间格式必须为 HH:MM")
                            days = positive("每隔几天运行一次（1 = 每天，0 返回）：")
                            if days is not None:
                                state.atomic(state.DATA/"schedule.json", {**(state.read(state.DATA/"schedule.json", {}) or {}), "time":value, "every_days":days})
                                print("已保存；选择启用后生效")
                    elif selected == "2":
                        saved = state.read(state.DATA/"schedule.json", {"time":jobs.schedule_status()["time"]})
                        jobs.schedule("enable", saved["time"], saved.get("every_days"))
                    else:
                        jobs.schedule("disable")
            elif choice == "8":
                settings_menu()
            elif choice == "9":
                doctor()
                selected = choose("1 打开浏览器检查登录  2 重新检查本机  0 返回：", set("012"))
                if selected == "1":
                    doctor(True)
                elif selected == "2":
                    doctor()
        except (ValueError, OSError, RuntimeError, jobs.BusyError, runner.ConditionsError, subprocess.SubprocessError) as error:
            print(f"操作未完成：{error}")


def notify_action(action, key=None, server=None):
    if action == "off":
        notify.disable()
        print("Bark 通知已关闭")
        return 0
    if action == "set":
        notify.save(key, server)
        print("Bark 已保存；任务失败时会推送。选择测试可确认能收到")
        return 0
    error = notify.send("X Download 测试", "Bark 通知配置正常")
    print(error or "测试通知已发送，请查看手机")
    return 1 if error else 0


def scheduled_run():
    """launchd fires daily; run only when at least every_days calendar days passed since the last success."""
    from datetime import date
    saved = state.read(state.DATA/"schedule.json", {}) or {}
    today, last = date.today(), saved.get("last_success")
    days = saved.get("every_days", 1)
    if last and (today-date.fromisoformat(last)).days < days:
        print(f"距上次成功运行（{last}）不足 {days} 天，今天跳过")
        return 0
    code = main(["run"])
    if code == 0:
        state.atomic(state.DATA/"schedule.json", {**saved, "last_success": today.isoformat()})
    return code


def main(argv=None):
    ap = parser()
    args = ap.parse_args(argv)
    try:
        if args.command in (None, "menu"):
            return menu()
        if args.command == "run":
            if args.scheduled:
                return jobs.scheduled(scheduled_run)
            config = options(args)
            if args.background:
                if args.dry_run:
                    raise ValueError("模拟运行请在前台执行")
                arguments = ["run", "--max", str(config["max"]), "--out", config["out"], "--media", config["media"], "--quality", config["quality"], "--delivery", config["delivery"]]
                if args.skip_scrape:
                    arguments.append("--skip-scrape")
                if args.use_preview:
                    arguments.append("--use-preview")
                return jobs.background(arguments)
            return runner.run(config)
        if args.command == "preview":
            return runner.preview(options(args))
        if args.command == "status":
            return show_status(args.watch)
        if args.command == "history":
            return show_history()
        if args.command == "logs":
            return show_logs(args.run, args.follow)
        if args.command == "retry":
            return runner.run(options(args), "download" if args.download_only else "delivery" if args.delivery_only else "all")
        if args.command == "stop":
            return jobs.stop()
        if args.command == "schedule":
            return jobs.schedule(args.action, getattr(args, "time", None), getattr(args, "every_days", None))
        if args.command == "notify":
            return notify_action(args.action, getattr(args, "key", None), getattr(args, "server", None))
        if args.command == "doctor":
            return doctor(args.login)
        if args.command == "open":
            return open_folder(args.target)
    except jobs.BusyError as error:
        print(f"任务跳过：{error}", file=sys.stderr)
        return 3
    except runner.ConditionsError as error:
        print(f"运行条件不满足：{error}", file=sys.stderr)
        return 4
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"操作失败：{error}", file=sys.stderr)
        return 2 if isinstance(error, ValueError) else 4
    except EOFError:
        print("\n已退出")
        return 0
    except KeyboardInterrupt:
        print("\n已退出；任务可在“查看任务、进度和日志”中查看")
        return 130
