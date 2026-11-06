import hashlib
import json
import plistlib
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from journalflow import scheduling
from journalflow.config import initialize, load


@pytest.fixture
def config(tmp_path):
    return load(initialize(tmp_path / "论文 library" / "journalflow.toml"))


def approve(config, **trial):
    gate = {"schema": 1, "config_sha256": hashlib.sha256(config.path.read_bytes()).hexdigest(),
            "baseline": {"passed": True}, "regional": {"passed": True},
            "trial": {"passed": True, "documents": 10, "kind": "live_download_conversion", **trial}}
    path = scheduling.gate_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(gate), encoding="utf-8")
    return path


def test_gate_blocks_missing_replayed_and_incomplete_trials(config):
    calls = []
    with pytest.raises(ValueError, match="gate.json"):
        scheduling.install(config, runner=lambda argv: calls.append(argv))
    approve(config, kind="baseline_holdout")
    with pytest.raises(ValueError, match="holdout"):
        scheduling.require_gate(config)
    approve(config, documents=9)
    with pytest.raises(ValueError, match="10 documents"):
        scheduling.require_gate(config)
    approve(config)
    assert scheduling.require_gate(config)["trial"]["documents"] == 10
    config.path.write_text(config.path.read_text() + "\n# changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="match this configuration"):
        scheduling.require_gate(config)
    assert not calls


def test_gate_rejects_non_object_and_missing_regional_validation(config):
    path = approve(config)
    path.write_text("[]", encoding="utf-8")
    assert not scheduling.gate_status(config)["ready"]
    approve(config)
    gate = json.loads(path.read_bytes())
    gate["regional"]["passed"] = False
    path.write_text(json.dumps(gate), encoding="utf-8")
    with pytest.raises(ValueError, match="regional"):
        scheduling.require_gate(config)


def test_export_definitions_do_not_enable_a_task_or_need_gate(config):
    schedule = scheduling.Schedule()
    xml = ET.fromstring(scheduling.render_windows_task(config, schedule, python=Path("C:/Python/python.exe")))
    ns = {"t": scheduling.TASK_NS}
    assert xml.find(".//t:DaysOfWeek/t:Monday", ns) is not None
    assert xml.findtext(".//t:StartBoundary", namespaces=ns).endswith("T10:00:00")
    assert xml.findtext(".//t:LogonType", namespaces=ns) == "InteractiveToken"
    assert "scheduling.py" in xml.findtext(".//t:Arguments", namespaces=ns)
    agent = plistlib.loads(scheduling.render_launchd(config, schedule))
    assert agent["StartCalendarInterval"] == {"Weekday": 1, "Hour": 10, "Minute": 0}
    assert not agent["RunAtLoad"]
    assert agent["ProgramArguments"][1].endswith("scheduling.py")
    assert str(config.path) in agent["ProgramArguments"]
    assert not scheduling._receipt_path(config).exists()


def test_project_identifier_is_scoped_to_config_path(config, tmp_path):
    second = load(initialize(tmp_path / "other" / "journalflow.toml"))
    assert scheduling.identifier(config) != scheduling.identifier(second)
    assert scheduling.identifier(config).startswith("org.journalflow.weekly.")


def test_windows_install_and_uninstall_touch_only_owned_task(config, monkeypatch):
    approve(config)
    monkeypatch.setattr(scheduling, "check_local_timezone", lambda value: None)
    calls = []
    registered = False

    def runner(argv):
        nonlocal registered
        calls.append(argv)
        if argv[0] == "whoami":
            return subprocess.CompletedProcess(argv, 0, stdout='"DOMAIN\\user","S-1-5-21-123"\n')
        if "/Create" in argv:
            registered = True
        if "/Query" in argv:
            if not registered:
                return subprocess.CompletedProcess(argv, 1, stdout="")
            return subprocess.CompletedProcess(argv, 0, stdout=scheduling.render_windows_task(config))
        return subprocess.CompletedProcess(argv, 0, stdout="")

    receipt = scheduling.install(config, platform="win32", runner=runner)
    assert receipt["id"] == scheduling.identifier(config)
    with pytest.raises(ValueError, match="already installed"):
        scheduling.install(config, platform="win32", runner=runner)
    assert scheduling.uninstall(config, runner=runner)["removed"]
    mutations = [argv for argv in calls if "/Create" in argv or "/Delete" in argv]
    assert len(mutations) == 2
    assert all(argv[argv.index("/TN") + 1] == receipt["id"] for argv in mutations)
    assert not scheduling._receipt_path(config).exists()


def test_uninstall_refuses_foreign_task_and_receipt_paths(config, monkeypatch):
    approve(config)
    name = scheduling.identifier(config)
    path = scheduling._receipt_path(config)
    receipt = {"id": name, "config": str(config.path), "platform": "win32",
               "definition": str(config.data_dir / "automation" / (name + ".xml")),
               "argv": scheduling.scheduled_argv(config)}
    path.write_text(json.dumps(receipt), encoding="utf-8")
    calls = []

    def runner(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=scheduling.render_windows_task(config).replace(name, "other"))

    with pytest.raises(ValueError, match="differs"):
        scheduling.uninstall(config, runner=runner)
    assert not any("/Delete" in argv for argv in calls)
    calls.clear()
    receipt["definition"] = str(config.path)
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="definition path"):
        scheduling.uninstall(config, runner=runner)
    assert not calls and config.path.exists()


def test_scheduled_run_rechecks_gate_before_spawning(config, monkeypatch):
    called = []
    monkeypatch.setattr(scheduling.subprocess, "run", lambda *args, **kwargs: called.append(args))
    approve(config, kind="baseline_holdout")
    with pytest.raises(ValueError, match="holdout"):
        scheduling.run_scheduled(config)
    assert not called


def test_macos_job_definition_and_uninstall_are_project_scoped(config, monkeypatch, tmp_path):
    approve(config)
    monkeypatch.setattr(scheduling, "check_local_timezone", lambda value: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(scheduling.os, "getuid", lambda: 501, raising=False)
    calls = []
    loaded = False

    def runner(argv):
        nonlocal loaded
        calls.append(argv)
        if argv[1] == "print":
            return subprocess.CompletedProcess(argv, 0 if loaded else 1, stdout="")
        loaded = argv[1] == "bootstrap"
        return subprocess.CompletedProcess(argv, 0, stdout="")

    receipt = scheduling.install(config, platform="darwin", runner=runner)
    definition = Path(receipt["definition"])
    assert definition.is_file() and loaded
    original = definition.read_bytes()
    foreign = plistlib.loads(original)
    foreign["ProgramArguments"] = ["/unrelated/program"]
    definition.write_bytes(plistlib.dumps(foreign))
    with pytest.raises(ValueError, match="differs"):
        scheduling.uninstall(config, runner=runner)
    assert loaded and definition.is_file()
    definition.write_bytes(original)
    assert scheduling.uninstall(config, runner=runner)["removed"]
    assert not loaded and not definition.exists()
    assert all(receipt["id"] in " ".join(argv) for argv in calls)


def test_schedule_uses_configured_calendar_defaults(config):
    base = config.path.read_text(encoding="utf-8").split("\n[automation]", 1)[0]
    config.path.write_text(base + '\n[automation]\nweekday=2\nhour=9\nminute=30\ntimezone="Asia/Shanghai"\n',
                           encoding="utf-8")
    assert scheduling.configured_schedule(config) == scheduling.Schedule(2, 9, 30)


def test_native_entry_works_without_installed_package_or_pythonpath(config):
    result = subprocess.run([sys.executable, "-I", "-S", str(Path(scheduling.__file__).resolve()),
                             "--config", str(config.path), "status"], capture_output=True, text=True,
                            cwd=config.path.parent, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["schedule"]["timezone"] == "Asia/Shanghai"
    assert not report["installed"] and not report["gate"]["ready"]


def test_native_scheduler_output_handles_windows_utf16(config, monkeypatch):
    value = scheduling.render_windows_task(config)
    monkeypatch.setattr(scheduling.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0, stdout=value.encode("utf-16"), stderr=b""))
    result = scheduling._run(["schtasks", "/Query"])
    assert result.stdout == value and result.stderr == ""


@pytest.mark.parametrize("schedule", [scheduling.Schedule(weekday=7), scheduling.Schedule(hour=24),
                                     scheduling.Schedule(minute=-1)])
def test_schedule_rejects_invalid_calendar_values(schedule):
    with pytest.raises(ValueError):
        schedule.validate()
