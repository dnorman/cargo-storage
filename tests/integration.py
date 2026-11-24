#!/usr/bin/env python3
"""Exercise routing on a disposable APFS disk image, never an existing physical drive."""
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import time
import uuid

import test_install


def run(*args, **kwargs):
    return subprocess.check_output(args, timeout=90, **kwargs)


def create_workspace(project, name):
    (project / "src").mkdir(parents=True)
    (project / "macros/src").mkdir(parents=True)
    (project / "Cargo.toml").write_text(
        '[package]\nname="storage-probe"\nversion="0.1.0"\nedition="2021"\n'
        '[dependencies]\nprobe-macros={path="macros"}\n[workspace]\nmembers=["macros"]\n')
    (project / "macros/Cargo.toml").write_text(
        '[package]\nname="probe-macros"\nversion="0.1.0"\nedition="2021"\n[lib]\nproc-macro=true\n')
    (project / "macros/src/lib.rs").write_text(
        'use proc_macro::TokenStream;\n#[proc_macro]\npub fn stamp(_:TokenStream)->TokenStream {'
        + json.dumps(json.dumps(name)) + '.parse().unwrap() }\n')
    generated = json.dumps('const MARK: &str = ' + json.dumps(name) + ';')
    (project / "build.rs").write_text(
        'fn main(){let p=std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap());'
        'std::fs::write(p.join("generated.rs"),' + generated + ').unwrap();}\n')
    (project / "src/main.rs").write_text(
        'include!(concat!(env!("OUT_DIR"),"/generated.rs"));\n'
        'fn main(){println!("{}:{}",MARK,probe_macros::stamp!());}\n'
        '#[test] fn correct(){assert_eq!(MARK,probe_macros::stamp!());}\n')


def wait_for(state, mode):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            route = json.loads(state.read_text())["route"]
            if route["mode"] == mode:
                return route
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise AssertionError("Watcher did not switch to " + mode)


def build(project, name, route, env, cargo, toolchain):
    command = [cargo] + (["+" + toolchain] if toolchain else [])
    result = subprocess.run(command + ["build", "--offline", "--message-format=json"],
                            cwd=project, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    messages = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    output = next(message["out_dir"] for message in messages if message["reason"] == "build-script-executed")
    assert output.startswith(route["root"] + "/"), output
    executable = next(message["executable"] for message in messages if message.get("executable"))
    assert Path(executable).is_relative_to(project / "target"), executable
    assert run(executable, text=True).strip() == name + ":" + name
    tested = subprocess.run(command + ["test", "--offline", "--quiet"], cwd=project, env=env,
                            capture_output=True, text=True, timeout=120)
    assert tested.returncode == 0, tested.stderr
    # Locate the workspace cache through its tag, not Cargo's version-specific layout.
    root = next(p for p in Path(output).parents if (p / "CACHEDIR.TAG").is_file())
    assert root.is_relative_to(route["root"])
    assert root.stat().st_dev == Path(route["root"]).stat().st_dev
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--toolchain", default="stable", help="rustup toolchain, or empty for bare cargo")
    args = parser.parse_args()
    binary = args.binary.resolve()
    cargo = shutil.which("cargo")
    if not cargo:
        raise SystemExit("Cargo is required")
    with tempfile.TemporaryDirectory(prefix="cargo-storage-integration-") as temporary:
        root = Path(temporary).resolve()
        image = root / "test.sparseimage"
        name = "Cargo Storage Test-" + uuid.uuid4().hex[:8]
        run("/usr/bin/hdiutil", "create", "-size", "256m", "-fs", "APFS", "-type", "SPARSE",
            "-volname", name, str(image))
        volume_id = None
        watcher = None
        log = (root / "watch.log").open("w")
        try:
            attached = plistlib.loads(run("/usr/bin/hdiutil", "attach", "-nobrowse", "-plist", str(image)))
            mount = Path(next(item["mount-point"] for item in attached["system-entities"] if "mount-point" in item))
            info = plistlib.loads(run("/usr/sbin/diskutil", "info", "-plist", str(mount)))
            volume_id = info["VolumeUUID"]
            cargo_home = root / "cargo-home"
            cargo_home.mkdir()
            config = cargo_home / "config.toml"
            config.write_text(test_install.installer.add_managed_setting('[profile.dev]\ndebug=0\n', root / "internal"))
            settings = root / "settings.json"
            settings.write_text(json.dumps(dict(volumeUUID=volume_id, externalSubdirectory="CargoBuilds/test",
                internalRoot=str(root / "internal"), cargoConfig=str(config), stateDirectory=str(root / "state"))))
            env = os.environ.copy()
            for key in ("CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR", "CARGO_BUILD_BUILD_DIR",
                        "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER"):
                env.pop(key, None)
            env["CARGO_HOME"] = str(cargo_home)
            for letter in ("a", "b"):
                create_workspace(root / letter, letter)
            watcher = subprocess.Popen([str(binary), "watch", str(settings)], stdout=log, stderr=log)
            state = root / "state/state.json"

            def check(mode):
                route = wait_for(state, mode)
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    jobs = [pool.submit(build, root / letter, letter, route, env, cargo, args.toolchain)
                            for letter in ("a", "b")]
                    directories = [job.result() for job in jobs]
                assert directories[0] != directories[1], directories
                print("PASS: " + mode + " routing, concurrent same-name workspaces, build scripts, proc macros and tests", flush=True)
                return route

            external = check("external")
            run("/usr/sbin/diskutil", "unmount", volume_id)
            check("internal")
            assert not Path(external["root"]).exists(), "Helper created a fake external mount directory"
            run("/usr/sbin/diskutil", "mount", volume_id)
            check("external")
            before = (config.stat().st_mtime_ns, state.stat().st_mtime_ns)
            time.sleep(4.5)
            assert before == (config.stat().st_mtime_ns, state.stat().st_mtime_ns)
            print("PASS: unchanged watcher ticks leave config and state untouched", flush=True)
        finally:
            if watcher:
                watcher.terminate()
                watcher.wait(timeout=10)
            log.close()
            # Detach only the image created inside this test's unique temp directory.
            images = plistlib.loads(run("/usr/bin/hdiutil", "info", "-plist")).get("images", [])
            for item in images:
                if Path(item.get("image-path", "")) == image:
                    device = next(e["dev-entry"] for e in item["system-entities"] if "dev-entry" in e)
                    run("/usr/bin/hdiutil", "detach", device)


if __name__ == "__main__":
    main()
