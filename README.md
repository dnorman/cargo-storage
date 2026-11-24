# cargo-storage

Keep Cargo's intermediate build files on an external SSD when it is connected,
with automatic fallback to internal storage when it is absent.

A small native macOS helper watches volume mount, unmount, rename and wake events.
It updates one marked setting in your Cargo configuration. Cargo gives each
workspace its own cache directory using its workspace-path hash.

| Drive state | Intermediate artifacts |
| --- | --- |
| Configured volume mounted and writable | /Volumes/(current name)/CargoBuilds/(user)/{workspace-path-hash} |
| Volume absent or unavailable | ~/.cargo/build/{workspace-path-hash} |

Final executables keep their usual project target location. Existing project
settings that change target-dir still apply.

## Requirements

- macOS 14 or later, with Xcode Command Line Tools to compile the helper.
- Python 3.11 or later for installation and tests.
- Cargo 1.91 or later for build.build-dir and workspace-path-hash support.
- An external volume with a persistent UUID. APFS is recommended for a Mac-only drive.

The daemon itself has no third-party dependencies. Python is only used for setup
and testing.

## Install

Clone this repository, then build:

    make build

Choose your mounted external volume. Preview the installation first if desired:

    python3 scripts/install.py --volume "/Volumes/Builds" --dry-run
    python3 scripts/install.py --volume "/Volumes/Builds"

Alternatively, pass its volume UUID with --volume-uuid. The volume need not be
connected when installing by UUID.

The installer uses your current home and Cargo home, identifies the volume by UUID,
backs up an existing config, and starts a per-user LaunchAgent. It adds the command
at ~/.local/bin/cargo-storage; put ~/.local/bin on your PATH or use that full path.
No sudo is required.

Optional installer arguments:

- --external-subdirectory: relative path on the volume; defaults to CargoBuilds/(user).
- --cargo-home: Cargo home to configure; defaults to CARGO_HOME or ~/.cargo.
- --dry-run: show the resolved paths without changing anything.

Installation refuses to overwrite an existing installation, a symlinked config,
a legacy Cargo config file, or an existing build-dir setting. Resolve those
explicitly before installing. It supports a plain [build] TOML table; unusual
table syntax is preserved by refusing the edit.

## Use

    cargo-storage status
    cargo-storage sync

Status shows the last selected route, helper status, and available health counters
for the system and external SSDs. These include SMART status, endurance used,
temperature, host writes and media errors when macOS exposes them. Missing
counters are reported as unavailable. Endurance used is a vendor estimate,
not a failure countdown.

Sync immediately reconciles the configuration. Watch runs the foreground watcher;
the LaunchAgent normally runs it for you. Each command can take an explicit
settings JSON path as its last argument.

## How it works

The helper changes only build.build-dir inside its marked block in
$CARGO_HOME/config.toml. It preserves other settings and refuses edits if the
managed block is missing, duplicated or damaged. Configuration replacement is
atomic; a lock serializes helper processes.

Drive identity comes from its volume UUID, so renaming it is supported. Events
trigger reconciliation, with a two-second poll as a fallback. Unchanged polls
do not rewrite config or state files. Health queries run only for status.

The generated settings are stored at:

    ~/Library/Application Support/CargoStorage/settings.json

The settings contain volumeUUID, externalSubdirectory, internalRoot, cargoConfig
and stateDirectory. Paths other than externalSubdirectory must be absolute;
colons, template braces and control characters are rejected in configured paths.

The helper, source copy, config backup and state also live in that support folder.
Logs are under ~/Library/Logs/CargoStorage/. The LaunchAgent is:

    ~/Library/LaunchAgents/net.danielnorman.cargo-storage.plist

## Scope and limitations

- Switching affects new Cargo invocations. A build already running keeps its old
  path. Finish builds and tests, then eject before disconnecting the SSD.
- Routing has a short event-processing window, normally immediate and up to the
  polling interval. It does not make abrupt removal safe.
- Caches on the two drives are independent. The helper does not migrate,
  synchronize, deduplicate or delete them, and the internal fallback has no size cap.
- Workspaces stay separate. This is central storage, not a shared cross-workspace
  compilation cache.
- Cargo config, environment and command-line overrides take their normal
  precedence. Other Cargo homes, users, containers and older Cargo are outside
  this installation's scope.
- All normal invocations using the configured Cargo home can benefit, including
  GUI tools and direct toolchain binaries; no shell wrapper is involved.
- This does not change debug information, incremental compilation or compiler-cache
  settings.
- The current installer is for fresh installations. It does not perform upgrades.

See Cargo's [configuration reference](https://doc.rust-lang.org/cargo/reference/config.html#buildbuild-dir)
for build-dir templates and precedence.

## Disable or uninstall

Stop the watcher first:

    launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/net.danielnorman.cargo-storage.plist"

Remove the marked cargo-storage block from the configured Cargo config. Preserve
other settings; do not restore an old full-file backup over subsequent edits.
Remove the LaunchAgent plist to prevent startup at login. The command symlink,
support directory and logs can then be removed. Build caches are independent and
can be kept or cleaned separately.

## Development and verification

    make test
    make integration

The unit and command tests cover config preservation, malformed markers,
path validation, symlink refusal, installation into a disposable home and
unchanged watcher ticks.

Integration creates its own small APFS disk image and isolated Cargo home. It
builds two same-named workspaces concurrently, with different code, build scripts
and procedural macros. It checks executable output and runs Rust tests on the
external route, internal fallback, and external route again after remounting.
It never unmounts an existing physical disk or changes your actual Cargo config.

The integration test uses the installed stable rustup toolchain by default. To
test another installed version:

    python3 tests/integration.py --binary .build/cargo-storage --toolchain 1.99.1

Generated binaries, logs, images, settings and machine state are excluded from Git.
