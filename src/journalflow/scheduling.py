"""Opt-in native weekly jobs, guarded by verified live-pipeline receipts."""
from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import io
import json
import locale
import os
import plistlib
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if __package__:
    from .config import Config, load
else:
    # Native schedulers do not inherit the double-click launcher's PYTHONPATH.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from journalflow.config import Config, load

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


@dataclass(frozen=True)
class Schedule:
    weekday: int = 0
    hour: int = 10
    minute: int = 0
    timezone: str = "Asia/Shanghai"

    def validate(self):
        if (any(type(value) is not int for value in (self.weekday, self.hour, self.minute)) or
                not 0 <= self.weekday <= 6 or not 0 <= self.hour <= 23 or not 0 <= self.minute <= 59):
            raise ValueError("Require weekday 0–6, hour 0–23 and minute 0–59")
        timezone_info(self.timezone)


def timezone_info(name):
    # Shanghai has no DST in the supported scheduling period. This also works
    # with Windows Python installations that do not include an IANA tz database.
    if name == "Asia/Shanghai":
        return timezone(timedelta(hours=8), name)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ValueError("Unknown timezone; install tzdata to use additional IANA zones") from error


def identifier(config: Config) -> str:
    key = os.path.normcase(str(config.path.resolve())).encode("utf-8")
    return "org.journalflow.weekly." + hashlib.sha256(key).hexdigest()[:12]


def gate_path(config):
    return config.data_dir / "automation/gate.json"


def configured_schedule(config):
    raw = tomllib.loads(config.path.read_text(encoding="utf-8")).get("automation", {})
    schedule = Schedule(**{key: raw[key] for key in Schedule.__dataclass_fields__ if key in raw})
    schedule.validate()
    return schedule


def require_gate(config):
    try:
        gate = json.loads(gate_path(config).read_bytes())
    except (OSError, ValueError) as error:
        raise ValueError("Weekly updates are pending: no valid automation/gate.json") from error
    expected = hashlib.sha256(config.path.read_bytes()).hexdigest()
    if not isinstance(gate, dict) or gate.get("schema") != 1 or gate.get("config_sha256") != expected:
        raise ValueError("Weekly updates are pending: gate does not match this configuration")
    for name in ("baseline", "trial", "regional"):
        if not isinstance(gate.get(name), dict) or gate[name].get("passed") is not True:
            raise ValueError(f"Weekly updates are pending: {name} validation has not passed")
    trial = gate["trial"]
    if trial.get("kind") != "live_download_conversion":
        raise ValueError("Weekly updates are pending: holdout replay cannot approve live download/conversion")
    if type(trial.get("documents")) is not int or trial["documents"] < 10:
        raise ValueError("Weekly updates are pending: the live trial requires at least 10 documents")
    return gate


def gate_status(config):
    try:
        require_gate(config)
        return {"ready": True, "reason": "Live trial and regional validation passed"}
    except ValueError as error:
        return {"ready": False, "reason": str(error)}


def scheduled_argv(config, limit=10, python=None):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Batch limit must be an integer between 1 and 1000")
    return [str(Path(python or sys.executable).resolve()), str(Path(__file__).resolve()),
            "--config", str(config.path), "run", "--limit", str(limit)]


def check_local_timezone(schedule):
    schedule.validate()
    zone = timezone_info(schedule.timezone)
    year = datetime.now().year
    for month in range(1, 13):
        local = datetime(year, month, 15, 12)
        if local.astimezone().utcoffset() != local.replace(tzinfo=zone).utcoffset():
            raise ValueError("Set the operating-system timezone to the selected schedule timezone before installing")


def render_windows_task(config, schedule=Schedule(), *, python=None, user_id=None):
    schedule.validate()
    ET.register_namespace("", TASK_NS)
    def element(parent, name, text=None):
        result = ET.SubElement(parent, f"{{{TASK_NS}}}{name}")
        if text is not None:
            result.text = str(text)
        return result
    root = ET.Element(f"{{{TASK_NS}}}Task", version="1.4")
    registration = element(root, "RegistrationInfo")
    element(registration, "Description", identifier(config))
    triggers = element(root, "Triggers")
    calendar = element(triggers, "CalendarTrigger")
    element(calendar, "StartBoundary", f"2026-01-05T{schedule.hour:02}:{schedule.minute:02}:00")
    element(calendar, "Enabled", "true")
    weekly = element(calendar, "ScheduleByWeek")
    element(weekly, "WeeksInterval", 1)
    element(element(weekly, "DaysOfWeek"), WEEKDAYS[schedule.weekday])
    principal = element(element(root, "Principals"), "Principal")
    principal.set("id", "JournalFlowUser")
    element(principal, "UserId", user_id or getpass.getuser())
    element(principal, "LogonType", "InteractiveToken")
    element(principal, "RunLevel", "LeastPrivilege")
    settings = element(root, "Settings")
    element(settings, "MultipleInstancesPolicy", "IgnoreNew")
    element(settings, "DisallowStartIfOnBatteries", "false")
    element(settings, "StopIfGoingOnBatteries", "false")
    element(settings, "StartWhenAvailable", "true")
    element(settings, "ExecutionTimeLimit", "PT0S")
    actions = element(root, "Actions")
    actions.set("Context", "JournalFlowUser")
    execute = element(actions, "Exec")
    argv = scheduled_argv(config, python=python)
    element(execute, "Command", argv[0])
    element(execute, "Arguments", subprocess.list2cmdline(argv[1:]))
    element(execute, "WorkingDirectory", config.path.parent)
    return ET.tostring(root, encoding="unicode")


def render_launchd(config, schedule=Schedule(), *, python=None):
    schedule.validate()
    return plistlib.dumps({
        "Label": identifier(config), "ProgramArguments": scheduled_argv(config, python=python),
        "WorkingDirectory": str(config.path.parent), "RunAtLoad": False,
        "StartCalendarInterval": {"Weekday": (schedule.weekday + 1) % 7,
                                  "Hour": schedule.hour, "Minute": schedule.minute},
        "StandardOutPath": str(config.data_dir / "automation/weekly.stdout.log"),
        "StandardErrorPath": str(config.data_dir / "automation/weekly.stderr.log"),
    }, sort_keys=True)


def _run(argv):
    result = subprocess.run(argv, capture_output=True, check=False, shell=False)
    for name in ("stdout", "stderr"):
        data = getattr(result, name)
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            value = data.decode("utf-16")
        elif b"\x00" in data[:100]:
            value = data.decode("utf-16-le")
        else:
            try:
                value = data.decode("utf-8-sig")
            except UnicodeError:
                value = data.decode(locale.getpreferredencoding(False), errors="replace")
        setattr(result, name, value)
    return result


def _receipt_path(config):
    return config.data_dir / "automation/schedule-receipt.json"


def install(config, schedule=None, *, platform=None, runner=_run):
    require_gate(config)
    schedule = schedule or configured_schedule(config)
    check_local_timezone(schedule)
    platform = platform or sys.platform
    name = identifier(config)
    receipt_path = _receipt_path(config)
    if receipt_path.exists():
        raise ValueError("This project's weekly job is already installed; uninstall it before changing the schedule")
    if platform == "win32":
        if runner(["schtasks", "/Query", "/TN", name]).returncode == 0:
            raise ValueError("A task with this identifier already exists; it will not be overwritten")
        sid = runner(["whoami", "/user", "/fo", "csv", "/nh"])
        if sid.returncode:
            raise ValueError("Cannot resolve the current Windows user SID")
        user_id = next(csv.reader(io.StringIO(sid.stdout)))[1].strip()
        definition = config.data_dir / "automation" / (name + ".xml")
        definition.parent.mkdir(parents=True, exist_ok=True)
        definition.write_text(render_windows_task(config, schedule, user_id=user_id), encoding="utf-16")
        result = runner(["schtasks", "/Create", "/TN", name, "/XML", str(definition)])
    elif platform == "darwin":
        definition = Path.home() / "Library/LaunchAgents" / (name + ".plist")
        if definition.exists():
            raise ValueError("A launch agent with this identifier already exists; it will not be overwritten")
        if runner(["launchctl", "print", f"gui/{os.getuid()}/" + name]).returncode == 0:
            raise ValueError("A launch agent with this identifier is already loaded; it will not be overwritten")
        definition.parent.mkdir(parents=True, exist_ok=True)
        (config.data_dir / "automation").mkdir(parents=True, exist_ok=True)
        definition.write_bytes(render_launchd(config, schedule))
        result = runner(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(definition)])
        if result.returncode:
            definition.unlink()
    else:
        raise ValueError("Native weekly scheduling supports Windows and macOS")
    if result.returncode:
        raise ValueError("Native scheduler rejected this project's job; check local scheduler permissions")
    receipt = {"schema": 1, "id": name, "config": str(config.path), "platform": platform,
               "definition": str(definition), "argv": scheduled_argv(config), "schedule": asdict(schedule)}
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return receipt


def uninstall(config, *, runner=_run):
    path = _receipt_path(config)
    if not path.exists():
        return {"removed": False, "reason": "This project has no schedule receipt"}
    receipt = json.loads(path.read_bytes())
    if receipt.get("id") != identifier(config) or receipt.get("config") != str(config.path):
        raise ValueError("Schedule receipt belongs to a different project")
    name = receipt["id"]
    definition = Path(receipt["definition"])
    if receipt["platform"] == "win32":
        expected = config.data_dir / "automation" / (name + ".xml")
        if definition.resolve() != expected.resolve():
            raise ValueError("Unexpected task definition path")
        current = runner(["schtasks", "/Query", "/TN", name, "/XML"])
        if current.returncode:
            raise ValueError("Cannot verify the registered task; its receipt and definition were retained")
        root = ET.fromstring(current.stdout)
        def read(name):
            return root.findtext(".//" + f"{{{TASK_NS}}}" + name)
        if (read("Description") != name or read("Command") != receipt["argv"][0] or
                read("Arguments") != subprocess.list2cmdline(receipt["argv"][1:])):
            raise ValueError("Registered task differs from this project's receipt; refusing to remove it")
        if runner(["schtasks", "/Delete", "/TN", name, "/F"]).returncode:
            raise ValueError("Cannot remove this project's Windows task")
    elif receipt["platform"] == "darwin":
        expected = Path.home() / "Library/LaunchAgents" / (name + ".plist")
        if definition.resolve() != expected.resolve():
            raise ValueError("Unexpected launch agent path")
        if definition.exists():
            agent = plistlib.loads(definition.read_bytes())
            if agent.get("Label") != name or agent.get("ProgramArguments") != receipt["argv"]:
                raise ValueError("Launch agent differs from this project's receipt; refusing to remove it")
            domain = f"gui/{os.getuid()}"
            stopped = runner(["launchctl", "bootout", domain, str(definition)])
            if stopped.returncode and runner(["launchctl", "print", domain + "/" + name]).returncode == 0:
                raise ValueError("Cannot stop this project's launch agent; its definition was retained")
    else:
        raise ValueError("Unknown scheduler receipt platform")
    definition.unlink(missing_ok=True)
    path.unlink()
    return {"removed": True, "id": name}


def run_scheduled(config, limit=10):
    require_gate(config)
    scheduled_argv(config, limit)  # Validate before constructing the child CLI request.
    receipt_path = _receipt_path(config)
    if receipt_path.exists():
        schedule = Schedule(**json.loads(receipt_path.read_bytes())["schedule"])
        check_local_timezone(schedule)
    command = [sys.executable, "-m", "journalflow", "--config", str(config.path),
               "pipeline", "--limit", str(limit)]
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    result = subprocess.run(command, cwd=config.path.parent, check=False, shell=False, env=environment)
    return result.returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("journalflow.toml"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("uninstall")
    execute = commands.add_parser("run")
    execute.add_argument("--limit", type=int, default=10)
    for name in ("export", "install"):
        child = commands.add_parser(name)
        child.add_argument("--weekday", type=int)
        child.add_argument("--hour", type=int)
        child.add_argument("--minute", type=int)
        child.add_argument("--timezone")
        if name == "export":
            child.add_argument("--platform", choices=("windows", "macos"), required=True)
            child.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = load(args.config)
        if args.command == "status":
            result = {"gate": gate_status(config), "installed": _receipt_path(config).exists(),
                      "schedule": asdict(configured_schedule(config))}
        elif args.command == "run":
            return run_scheduled(config, args.limit)
        elif args.command == "uninstall":
            result = uninstall(config)
        else:
            values = asdict(configured_schedule(config))
            values.update({key: getattr(args, key) for key in values if getattr(args, key) is not None})
            schedule = Schedule(**values)
            if args.command == "install":
                result = install(config, schedule)
            else:
                value = (render_windows_task(config, schedule).encode("utf-16") if args.platform == "windows"
                         else render_launchd(config, schedule))
                with args.output.open("xb") as stream:
                    stream.write(value)
                result = {"definition": str(args.output), "enabled": False, "gate": gate_status(config)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, ET.ParseError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
