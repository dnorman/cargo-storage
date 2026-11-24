#!/usr/bin/env python3
"""Install cargo-storage for the current user; no administrator privileges needed."""
import argparse
import datetime
import getpass
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile
import tomllib
import uuid

REPO = Path(__file__).resolve().parent.parent
LABEL = "net.danielnorman.cargo-storage"
BEGIN = "# BEGIN cargo-storage managed build-dir"
END = "# END cargo-storage managed build-dir"


def add_managed_setting(original, internal):
    """Preserve the original text and verify the parsed change is exactly one setting."""
    parsed = tomllib.loads(original)
    if BEGIN in original or END in original:
        raise ValueError("Cargo config already has cargo-storage markers")
    build = parsed.get("build", {})
    if not isinstance(build, dict) or "build-dir" in build:
        raise ValueError("An existing build-dir needs manual reconciliation")
    value = str(internal) + "/{workspace-path-hash}"
    block = BEGIN + "\nbuild-dir = " + json.dumps(value, ensure_ascii=False) + "\n" + END + "\n"
    header = re.search(r"(?m)^[ \t]*\[build\][ \t]*$", original)
    if header:
        updated = original[:header.end()] + "\n" + block + original[header.end():]
    elif "build" in parsed:
        raise ValueError("Use a plain [build] table before installing; unusual TOML syntax is preserved")
    else:
        updated = original.rstrip() + "\n\n[build]\n" + block
    expected = dict(parsed)
    expected["build"] = dict(build, **{"build-dir": value})
    if tomllib.loads(updated) != expected:
        raise ValueError("Could not add the setting without changing other Cargo settings")
    return updated


def atomic_write(path, data, mode=0o600):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".cargo-storage-install-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_subdirectory(value):
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("External subdirectory must be a relative path without empty or dot components")
    validate_path_text(value)
    return value


def validate_path_text(value):
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or any(c in value for c in ":{}"):
        raise ValueError("Paths cannot contain control characters, colons or Cargo template braces")


def volume_uuid(volume):
    data = subprocess.check_output(["/usr/sbin/diskutil", "info", "-plist", str(volume)])
    info = plistlib.loads(data)
    value = info.get("VolumeUUID")
    if not value or not info.get("Mounted"):
        raise ValueError("Choose a mounted volume with a persistent volume UUID")
    return str(uuid.UUID(value)).upper()


def make_plan(home, cargo_home, drive_uuid, subdirectory, binary):
    home, cargo_home = Path(home).absolute(), Path(cargo_home).absolute()
    for path in (home, cargo_home):
        validate_path_text(str(path))
    drive_uuid = str(uuid.UUID(drive_uuid)).upper()
    validate_subdirectory(subdirectory)
    support = home / "Library/Application Support/CargoStorage"
    config = cargo_home / "config.toml"
    plist = home / "Library/LaunchAgents" / (LABEL + ".plist")
    command = home / ".local/bin/cargo-storage"
    for path in (support, plist, command):
        if path.exists() or path.is_symlink():
            raise ValueError(f"Installation destination already exists: {path}")
    if config.is_symlink() or (config.exists() and not config.is_file()):
        raise ValueError("Cargo config must be a regular file")
    legacy = cargo_home / "config"
    if legacy.exists() or legacy.is_symlink():
        raise ValueError("Cargo uses a legacy config file; reconcile it before installing")
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("Run make build first")
    original = config.read_bytes() if config.exists() else None
    internal = cargo_home / "build"
    updated = add_managed_setting((original or b"").decode(), internal).encode()
    settings = dict(volumeUUID=drive_uuid, externalSubdirectory=subdirectory,
                    internalRoot=str(internal), cargoConfig=str(config),
                    stateDirectory=str(support / "state"))
    return dict(home=home, support=support, config=config, plist=plist, command=command,
                logs=home / "Library/Logs/CargoStorage", binary=binary,
                original=original, updated=updated, settings=settings)


def install(plan, start=True):
    support, config = plan["support"], plan["config"]
    # The plan is also used by --dry-run; check it again immediately before writing.
    for path in (support, plan["plist"], plan["command"]):
        if path.exists() or path.is_symlink():
            raise ValueError(f"Installation destination appeared: {path}")
    current = config.read_bytes() if config.exists() else None
    if current != plan["original"] or config.is_symlink():
        raise ValueError("Cargo config changed after preflight")
    support.mkdir(parents=True, mode=0o700)
    for folder in (config.parent, plan["logs"], plan["plist"].parent, plan["command"].parent):
        folder.mkdir(parents=True, exist_ok=True)
    original_mode = config.stat().st_mode & 0o777 if config.exists() else 0o600
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    if plan["original"] is not None:
        atomic_write(support / ("cargo-config.before-" + timestamp + ".toml"), plan["original"])
    for source, name in ((plan["binary"], "cargo-storage"),
                         (REPO / "Sources/CargoStorage.swift", "CargoStorage.swift"),
                         (REPO / "README.md", "README.md")):
        shutil.copy2(source, support / name)
    os.chmod(support / "cargo-storage", 0o755)
    settings_file = support / "settings.json"
    atomic_write(settings_file, (json.dumps(plan["settings"], indent=2) + "\n").encode())
    plist = dict(Label=LABEL, ProgramArguments=[str(support / "cargo-storage"), "watch", str(settings_file)],
                 RunAtLoad=True, KeepAlive=True, ThrottleInterval=10, ProcessType="Background",
                 StandardOutPath=str(plan["logs"] / "stdout.log"),
                 StandardErrorPath=str(plan["logs"] / "stderr.log"))
    atomic_write(plan["plist"], plistlib.dumps(plist), 0o644)
    if (config.read_bytes() if config.exists() else None) != plan["original"]:
        raise ValueError("Cargo config changed during staging; configuration was not replaced")
    atomic_write(config, plan["updated"], original_mode)
    try:
        subprocess.run([str(support / "cargo-storage"), "sync", str(settings_file)], check=True)
        if start:
            subprocess.run(["/bin/launchctl", "bootstrap", "gui/" + str(os.getuid()),
                            str(plan["plist"])], check=True)
        plan["command"].symlink_to(support / "cargo-storage")
    except Exception:
        # Retain a working internal route. Never restore a whole old config over
        # unrelated edits someone may have made during installation.
        current = config.read_text()
        pattern = re.escape(BEGIN) + r"\nbuild-dir = [^\n]*\n" + re.escape(END)
        block = BEGIN + "\nbuild-dir = " + json.dumps(
            plan["settings"]["internalRoot"] + "/{workspace-path-hash}") + "\n" + END
        if len(re.findall(pattern, current)) == 1:
            atomic_write(config, re.sub(pattern, lambda _: block, current).encode(), original_mode)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    volume = parser.add_mutually_exclusive_group(required=True)
    volume.add_argument("--volume", type=Path, help="Mounted external volume, e.g. /Volumes/Builds")
    volume.add_argument("--volume-uuid", help="Persistent volume UUID; may be disconnected")
    parser.add_argument("--external-subdirectory", default="CargoBuilds/" + getpass.getuser())
    parser.add_argument("--cargo-home", type=Path,
                        default=Path(os.environ.get("CARGO_HOME", str(Path.home() / ".cargo"))))
    parser.add_argument("--dry-run", action="store_true", help="Show the plan without changing files")
    args = parser.parse_args()
    drive_uuid = volume_uuid(args.volume) if args.volume else args.volume_uuid
    plan = make_plan(Path.home(), args.cargo_home.expanduser(), drive_uuid,
                     args.external_subdirectory, REPO / ".build/cargo-storage")
    print(json.dumps(dict(settings=plan["settings"], launchAgent=str(plan["plist"]),
                          command=str(plan["command"])), indent=2))
    if not args.dry_run:
        install(plan)
        print("Installed. Run ~/.local/bin/cargo-storage status")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
