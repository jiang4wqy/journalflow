"""Build a paper-free desktop ZIP with three independent offline runtimes.

Prepare runtimes first with --prepare-only; run again after the source is frozen
to create the ZIP. Build inputs, wheel caches and private configuration are never
included in the public payload.
"""
from __future__ import annotations

import argparse
import email
import hashlib
import json
import shutil
import stat
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.tags import mac_platforms
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

PROJECT = Path(__file__).resolve().parents[1]
TARGETS = ("windows-x64", "macos-arm64", "macos-x64")
OVERLAY = {"pypdfium2": "5.14.0", "filelock": "3.32.7"}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def compatible(wheel, target):
    _name, _version, _build, tags = parse_wheel_filename(wheel.name)
    for tag in tags:
        if tag.platform == "any":
            return True
        if target == "windows-x64" and tag.platform == "win_amd64":
            return True
        if target.startswith("macos") and tag.platform.startswith("macosx_"):
            arch = "arm64" if target.endswith("arm64") else "x86_64"
            parts = tag.platform.split("_")
            if tag.platform.endswith(("_" + arch, "_universal2")) and int(parts[1]) <= 14:
                return True
    return False


def get_wheels(target, caches, work):
    work.mkdir(parents=True, exist_ok=True)
    found = {}
    for folder in [work, *caches]:
        for source in (folder, folder / target / "wheels", folder / target):
            for wheel in sorted(source.glob("*.whl")):
                name, version, _build, _tags = parse_wheel_filename(wheel.name)
                name = canonicalize_name(name)
                if name in found or OVERLAY.get(name) != str(version) or not compatible(wheel, target):
                    continue
                destination = work / wheel.name
                if source.resolve() != work.resolve():
                    shutil.copy2(wheel, destination)
                found[name] = destination
    missing = [f"{name}=={version}" for name, version in OVERLAY.items() if name not in found]
    if missing:
        command = [sys.executable, "-m", "pip", "--isolated", "--disable-pip-version-check", "download",
                   "--index-url", "https://pypi.org/simple", "--only-binary=:all:", "--no-deps",
                   "--python-version", "3.12", "--implementation", "cp", "--abi", "cp312",
                   "--dest", str(work)]
        platforms = ["win_amd64"] if target == "windows-x64" else list(
            mac_platforms((14, 0), "arm64" if target.endswith("arm64") else "x86_64"))
        for platform in platforms:
            command.extend(("--platform", platform))
        with (work / "download.log").open("w", encoding="utf-8") as log:
            completed = subprocess.run(command + missing, stdout=log, stderr=subprocess.STDOUT, check=False)
        if completed.returncode:
            raise ValueError("Cannot prepare public binary wheels for " + target)
        return get_wheels(target, [], work)
    return found


def overlay_wheel(wheel, site, stage):
    with zipfile.ZipFile(wheel) as archive:
        metadata = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        message = email.message_from_bytes(archive.read(metadata))
        name = canonicalize_name(message["Name"])
        folders = [site / name, *site.glob(name + "-*.dist-info")]
        if name == "pypdfium2":
            folders.append(site / "pypdfium2_raw")
        for folder in folders:
            if folder.is_dir():
                if not folder.resolve().is_relative_to(stage.resolve()):
                    raise ValueError("Refusing to replace packages outside the build copy")
                shutil.rmtree(folder)
        for item in archive.infolist():
            if item.is_dir():
                continue
            relative = PurePosixPath(item.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe wheel path")
            if any(part.endswith(".data") for part in relative.parts):
                _base, kind, *remaining = relative.parts
                if kind not in {"purelib", "platlib"}:
                    continue
                relative = PurePosixPath(*remaining)
            if relative.name == "direct_url.json" or "__pycache__" in relative.parts:
                continue
            destination = site.joinpath(*relative.parts)
            if not destination.resolve().is_relative_to(site.resolve()):
                raise ValueError("Unsafe extracted wheel path")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
    license_text = message.get("License-Expression") or message.get("License") or "See bundled package license files"
    return {"name": message["Name"], "version": message["Version"], "wheel": wheel.name,
            "bytes": wheel.stat().st_size, "sha256": digest(wheel),
            "license": " ".join(license_text.split())[:300]}


def check_runtime(root, target, receipt):
    site = root / "python" / ("Lib/site-packages" if target == "windows-x64" else "lib/python3.12/site-packages")
    messages = {}
    for path in site.glob("*.dist-info/METADATA"):
        message = email.message_from_bytes(path.read_bytes())
        name = canonicalize_name(message["Name"])
        if name in messages:
            raise ValueError("Duplicate installed package metadata: " + name)
        messages[name] = message
    locked = dict(item.split("==", 1) for item in receipt["requirements"])
    if {name: message["Version"] for name, message in messages.items()} != locked:
        raise ValueError("Runtime metadata does not match its lock")
    env = default_environment()
    env.update(python_version="3.12", python_full_version="3.12.15", extra="",
               sys_platform="win32" if target == "windows-x64" else "darwin",
               platform_system="Windows" if target == "windows-x64" else "Darwin",
               os_name="nt" if target == "windows-x64" else "posix")
    for name, message in messages.items():
        for raw in message.get_all("Requires-Dist", []):
            required = Requirement(raw)
            extras = ("", "crypto") if name == "pyjwt" else ("",)
            if required.marker and not any(required.marker.evaluate({**env, "extra": extra}) for extra in extras):
                continue
            child = messages.get(canonicalize_name(required.name))
            if child is None or Version(child["Version"]) not in required.specifier:
                raise ValueError("Runtime dependency conflict: " + name + ": " + raw)
    project = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    roots = project["dependencies"] + project["optional-dependencies"]["mcp"] + project["optional-dependencies"]["regional"]
    for raw in roots:
        required = Requirement(raw)
        if required.marker and not required.marker.evaluate(env):
            continue
        if Version(messages[canonicalize_name(required.name)]["Version"]) not in required.specifier:
            raise ValueError("Runtime does not meet the current project requirement: " + raw)
    checked = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name == "direct_url.json" or path.name in {"auth.json", "model.json", ".env"}:
            raise ValueError("Private/developer metadata found in a runtime payload")
        if target == "windows-x64" and path.suffix.lower() in {".exe", ".dll", ".pyd"}:
            with path.open("rb") as stream:
                header = stream.read(64)
                if header[:2] != b"MZ":
                    raise ValueError("Invalid Windows binary")
                stream.seek(int.from_bytes(header[60:64], "little"))
                header = stream.read(6)
            if header[:4] != b"PE\0\0" or int.from_bytes(header[4:6], "little") != 0x8664:
                raise ValueError("Wrong Windows binary architecture")
            checked += 1
        elif target.startswith("macos") and (path.suffix in {".so", ".dylib"} or path.name == "python3.12"):
            with path.open("rb") as stream:
                header = stream.read(8192)
            if header[:4] == bytes.fromhex("cffaedfe"):
                cpus = [int.from_bytes(header[4:8], "little")]
            elif header[:4] == bytes.fromhex("cafebabe"):
                cpus = [int.from_bytes(header[8 + i * 20:12 + i * 20], "big")
                        for i in range(int.from_bytes(header[4:8], "big"))]
            else:
                raise ValueError("Invalid Mac binary")
            if (0x100000C if target.endswith("arm64") else 0x1000007) not in cpus:
                raise ValueError("Wrong Mac binary architecture")
            checked += 1
    for name, link in receipt.get("symlinks", {}).items():
        path = root.joinpath(*PurePosixPath(name).parts)
        destination = (path.parent / link).resolve()
        if (link.startswith("/") or not destination.is_relative_to(root.resolve()) or
                not destination.is_file() or path.read_text(encoding="utf-8") != link):
            raise ValueError("Unsafe or broken runtime symlink")
    return {"target": target, "packages": len(messages), "native_binaries_checked": checked,
            "symlinks_checked": len(receipt.get("symlinks", {})), "dependency_closure": True,
            "mac_real_machine_tested": False, "passed": True}


def prepare(args, target):
    destination = args.staging / "JournalFlow/runtime" / target
    ready = destination / "desktop-runtime-receipt.json"
    if ready.is_file():
        receipt = json.loads((destination / "dependency-receipt.json").read_bytes())
        return check_runtime(destination, target, receipt)
    if destination.exists():
        raise ValueError("Incomplete runtime staging needs inspection: " + str(destination))
    source = args.runtime_root / target
    if not (source / "dependency-receipt.json").is_file():
        raise ValueError("Missing audited input runtime: " + target)
    wheels = get_wheels(target, args.wheel_cache, args.staging / "wheels" / target)
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    site = destination / "python" / ("Lib/site-packages" if target == "windows-x64" else "lib/python3.12/site-packages")
    overlays = [overlay_wheel(path, site, args.staging) for path in wheels.values()]
    receipt = json.loads((destination / "dependency-receipt.json").read_bytes())
    pins = dict(item.split("==", 1) for item in receipt["requirements"])
    pins.update(OVERLAY)
    receipt["requirements"] = [f"{name}=={version}" for name, version in sorted(pins.items())]
    receipt["wheels"] = [item for item in receipt["wheels"] if canonicalize_name(item["name"]) not in OVERLAY] + overlays
    receipt["desktop_core_overlay"] = OVERLAY
    receipt["static_verification"] = check_runtime(destination, target, receipt)
    (destination / "dependency-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    (destination / "requirements.lock.txt").write_text("\n".join(receipt["requirements"]) + "\n", encoding="utf-8")
    notices = ["# Bundled runtime components", "", "Python notices: python/LICENSE.txt.",
               "Retain every bundled dependency's metadata, license and copyright notices.",
               "PyMuPDF declares GNU Affero GPL 3.0 / Artifex commercial dual licensing;",
               "its component license and corresponding-source requirements remain applicable.", ""]
    notices += [f"- {item['name']} {item['version']}: {item.get('license', 'See bundled license files')}" for item in receipt["wheels"]]
    (destination / "THIRD_PARTY_NOTICES.md").write_text("\n".join(notices) + "\n", encoding="utf-8")
    ready.write_text(json.dumps(receipt["static_verification"], indent=2) + "\n", encoding="utf-8")
    return receipt["static_verification"]


def source_files():
    files = {}
    for folder, suffixes in (("src", {".py", ".toml"}), ("docs", {".md"}),
                             ("docs/assets", {".png", ".jpg", ".webp", ".svg"}),
                             ("examples", {".toml", ".md"}),
                             ("scripts", {".py", ".cmd", ".command", ".ps1", ".sh"})):
        for path in (PROJECT / folder).rglob("*"):
            if path.is_file() and path.suffix in suffixes and "__pycache__" not in path.parts:
                files[path.relative_to(PROJECT).as_posix()] = path
    for name in ("pyproject.toml", "LICENSE", ".gitignore", ".env.example"):
        path = PROJECT / name
        if path.is_file():
            files[name] = path
    for path in PROJECT.glob("README*.md"):
        files[path.name] = path
    return files


def package(args, reports):
    files = source_files()
    symlinks = {}
    for target in TARGETS:
        root = args.staging / "JournalFlow/runtime" / target
        receipt = json.loads((root / "dependency-receipt.json").read_bytes())
        for name, link in receipt.get("symlinks", {}).items():
            symlinks[f"runtime/{target}/{name}"] = link
        for path in root.rglob("*"):
            if path.is_file():
                files[f"runtime/{target}/" + path.relative_to(root).as_posix()] = path
    args.output.parent.mkdir(parents=True, exist_ok=True)
    partial = args.output.with_suffix(".building")
    if args.output.exists() or partial.exists():
        raise ValueError("Refusing to overwrite an existing output or partial archive")
    manifest = {"version": tomllib.loads((PROJECT / "pyproject.toml").read_text())["project"]["version"],
                "edition": "paper-free-desktop", "papers_bundled": False, "credentials_bundled": False,
                "generation_or_vision_models_bundled": False, "runtimes": reports, "files": []}
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as archive:
        for name, path in sorted(files.items()):
            mode = (stat.S_IFLNK | 0o777) if name in symlinks else stat.S_IFREG | (
                0o755 if name.endswith((".command", ".sh", ".so", ".dylib")) or "/bin/" in name else 0o644)
            info = zipfile.ZipInfo("JournalFlow/" + name)
            info.create_system = 3
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            info._compresslevel = 3
            if name in symlinks:
                data = symlinks[name].encode("utf-8")
                archive.writestr(info, data)
                value, size = hashlib.sha256(data).hexdigest(), len(data)
            else:
                value, size = digest(path), path.stat().st_size
                with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
            manifest["files"].append({"file": name, "sha256": value, "bytes": size, "symlink": symlinks.get(name)})
        archive.writestr("JournalFlow/bundle-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(partial) as archive:
        if archive.testzip():
            raise ValueError("Desktop archive failed CRC verification")
    partial.rename(args.output)
    args.output.with_suffix(args.output.suffix + ".sha256").write_text(digest(args.output) + "  " + args.output.name + "\n", encoding="utf-8")
    print(json.dumps({"archive": str(args.output), "bytes": args.output.stat().st_size,
                      "files": len(files), "symlinks": len(symlinks), "papers_bundled": False}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--staging", type=Path, default=PROJECT / "dist/.desktop-build")
    parser.add_argument("--wheel-cache", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, default=PROJECT / "dist/journalflow-desktop.zip")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    args.staging, args.runtime_root, args.output = args.staging.resolve(), args.runtime_root.resolve(), args.output.resolve()
    if (args.staging == PROJECT or args.staging.is_relative_to(PROJECT / "src") or
            args.staging.is_relative_to(args.runtime_root)):
        raise ValueError("Build staging must be separate from source code and audited input runtimes")
    reports = []
    for target in TARGETS:
        reports.append(prepare(args, target))
        print(json.dumps({"prepared": target, "packages": reports[-1]["packages"]}), flush=True)
    if not args.prepare_only:
        package(args, reports)


if __name__ == "__main__":
    main()
