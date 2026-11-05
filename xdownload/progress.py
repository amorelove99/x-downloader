import json
import re
import sys
import time
from . import state

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def clean(value):
    return ANSI.sub("", str(value))


class Reporter:
    def __init__(self, run=None, directory=None):
        self.run = run
        self.directory = directory
        self.last = 0
        self.live = None
        self.item_index = 0
        self.item_total = None
        self.last_plain_progress = 0
        self.file_key = None
        if sys.stdout.isatty():
            from rich.live import Live
            from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn
            self.overall = Progress(TextColumn("收藏"), BarColumn(), TextColumn("{task.completed:.0f}/{task.total:.0f}"), expand=True)
            self.overall_id = self.overall.add_task("收藏", total=0)
            self.file_progress = Progress(SpinnerColumn(), TextColumn("文件"), BarColumn(), DownloadColumn(), TransferSpeedColumn(), TimeRemainingColumn(), expand=True)
            self.file_id = self.file_progress.add_task("文件", total=None)
            self.live = Live("", refresh_per_second=4)
            self.live.start()

    def emit(self, phase, message, transient=False, **values):
        message = clean(message)
        event = {"at": state.now(), "phase": phase, "message": message, **values}
        current = time.monotonic()
        if transient and current - self.last < .25:
            return
        self.last = current
        if self.run is not None:
            persisted = state.read(state.DATA / "current.json", {})
            if persisted.get("run_id") == self.run["run_id"] and persisted.get("children"):
                self.run["children"] = persisted["children"]
            if "downloaded_bytes" not in values:
                for key in ("downloaded_bytes", "total_bytes", "speed", "file_key"):
                    self.run.pop(key, None)
            self.run.update(phase=phase, message=message, updated=state.now(), **values)
            state.atomic(state.DATA / "current.json", self.run)
        if self.directory and not transient:
            with (self.directory / "events.jsonl").open("a") as output:
                output.write(json.dumps(event, ensure_ascii=False) + "\n")
            with (self.directory / "run.log").open("a") as output:
                output.write(f"{event['at']} [{phase}] {message}\n")
        if self.live:
            from rich.text import Text
            from rich.console import Group
            from rich.panel import Panel
            if "item_total" in values:
                self.overall.update(self.overall_id, total=values["item_total"], completed=max(0, values["item_index"]-1))
            if phase == "完成" and self.overall.tasks[self.overall_id].total:
                self.overall.update(self.overall_id, completed=self.overall.tasks[self.overall_id].total)
            components = [Text(message)]
            if self.overall.tasks[self.overall_id].total:
                components.append(self.overall)
            if "downloaded_bytes" in values:
                file_key = values.get("file_key")
                if file_key != self.file_key or values["downloaded_bytes"] < self.file_progress.tasks[self.file_id].completed:
                    self.file_progress.reset(self.file_id, total=values.get("total_bytes"))
                    self.file_key = file_key
                self.file_progress.update(self.file_id, total=values.get("total_bytes"), completed=values["downloaded_bytes"])
                components.append(self.file_progress)
            self.live.update(Panel(Group(*components), title=f"X Download · {phase}"))
            if not transient:
                self.live.console.print(f"[{phase}] {message}", markup=False)
        else:
            if not transient or current - self.last_plain_progress >= 2:
                print(f"[{phase}] {message}", flush=True)
                if transient:
                    self.last_plain_progress = current

    def close(self):
        if self.live:
            self.live.stop()
