"""Putting Bothy on a machine and taking it off again, without a conversation.

Install is a feature here, not packaging. The closest comparable project is
excellent and effectively undeployable to a third party: a 3,945-line installer
that reads from /dev/tty so even a piped shell gets a wizard, a setup command
that refuses to run headless, no --yes, and 939 configuration keys with no way
to ship one vetted profile to many sites. Its own maintainers name that as the
main obstacle to handing it to anyone.

So: one command, no prompts, no TTY, idempotent, non-zero on failure.

THE EXIT-CODE CONTRACT, AND HOW IT SURVIVES launchd. Bothy exits 75 to mean
"restart me" and 78 to mean "stop, restarting cannot help". systemd expresses
that directly with RestartPreventExitStatus=78. launchd has no equivalent — its
KeepAlive cannot say "unless it exited 78" — so the wrapper translates: an exit
of 78 becomes an exit of 0, which ``SuccessfulExit=false`` reads as "do not
restart". The contract is preserved; only its spelling changes.

THREE THINGS launchd DOES NOT DO that the wrapper has to:
  there is no ordering graph, so it waits for the tailscaled socket itself
  there is no EnvironmentFile, so it sources a mode-0600 file
  there is no log rotation, so newsyslog is configured alongside
Secrets never go in the plist: it is world-readable, and a token in it is a
token disclosed to every account on the machine.
"""

from __future__ import annotations

import dataclasses
import os
import platform
import shutil
import subprocess
from pathlib import Path

__all__ = ["Target", "plan", "LAUNCHD", "SYSTEMD", "render_wrapper", "render_launchd", "render_systemd"]

LAUNCHD = "launchd"
SYSTEMD = "systemd"


@dataclasses.dataclass(frozen=True)
class Target:
    """Where and how Bothy will be installed."""

    kind: str                       # launchd | systemd
    label: str                      # dev.bothy.<site>
    unit_path: Path
    wrapper_path: Path
    env_path: Path
    log_dir: Path
    repo: Path
    home: Path
    site: str
    user: str

    def as_dict(self) -> dict[str, str]:
        return {key: str(value) for key, value in dataclasses.asdict(self).items()}


def plan(*, site: str, home: Path, repo: Path, user: str | None = None, kind: str | None = None) -> Target:
    """Decide the install layout for this machine, without touching it."""
    system = (kind or ("launchd" if platform.system() == "Darwin" else "systemd")).lower()
    label = f"dev.bothy.{site}"
    who = user or os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"
    if system == LAUNCHD:
        return Target(
            kind=LAUNCHD, label=label,
            unit_path=Path(f"/Library/LaunchDaemons/{label}.plist"),
            wrapper_path=Path("/usr/local/libexec/bothy-run"),
            env_path=Path("/usr/local/etc/bothy.env"),
            log_dir=Path("/var/log/bothy"),
            repo=repo, home=home, site=site, user=who,
        )
    return Target(
        kind=SYSTEMD, label=label,
        unit_path=Path(f"/etc/systemd/system/{label}.service"),
        wrapper_path=Path("/usr/local/libexec/bothy-run"),
        env_path=Path("/usr/local/etc/bothy.env"),
        log_dir=Path("/var/log/bothy"),
        repo=repo, home=home, site=site, user=who,
    )


# The two service managers need OPPOSITE handling of exit 78, which is not
# obvious and cost a real debugging session to find.
#
# systemd has RestartPreventExitStatus=78 and honours it directly, so the code
# must reach systemd UNCHANGED. An earlier version translated 78 to 0 for both,
# and because the unit also says Restart=always — which restarts on a CLEAN
# exit too — systemd cheerfully restarted the very thing that had just said
# "restarting cannot help". The translation defeated the mechanism it was
# imitating.
#
# launchd has no equivalent, so there 78 becomes 0 and KeepAlive with
# SuccessfulExit=false reads that as "do not restart".
_TRANSLATE_78 = """
# launchd cannot express "restart unless it exited 78", so 78 is translated
# into a clean exit, which KeepAlive/SuccessfulExit=false reads as "do not
# restart". systemd needs the opposite: see the note in install.py.
if [ "$code" -eq 78 ]; then
    echo "bothy: exited 78 (configuration); not restarting" >&2
    exit 0
fi
exit "$code"
"""

_PASS_THROUGH = """
# Passed through unchanged so RestartPreventExitStatus=78 in the unit can see
# it. Translating it here would hide it from the mechanism that acts on it.
exit "$code"
"""


def render_wrapper(target: Target) -> str:
    """The launcher both service managers actually run.

    It exists because a service definition cannot express any of this: waiting
    for a dependency that has no ordering relationship, reading secrets from a
    file instead of from a world-readable plist, and — on launchd only —
    translating an exit code into a vocabulary it understands.
    """
    tail = _TRANSLATE_78 if target.kind == LAUNCHD else _PASS_THROUGH
    return f'''#!/bin/sh
# Generated by bothy install. Edits are lost on the next install.
set -eu

# Secrets live here, mode 0600, owned by the account that runs Bothy. Never in
# the service definition: a launchd plist is world-readable, and a token in one
# is a token disclosed to every account on the machine.
if [ -f "{target.env_path}" ]; then
    set -a
    . "{target.env_path}"
    set +a
fi

export BOTHY_HOME="{target.home}"

# launchd has no ordering graph and systemd's After= does not wait for the
# socket to be usable, only for the unit to be active. Bothy binds loopback and
# is reached through tailscale serve, so starting before tailscaled is up means
# an install that looks healthy and answers nothing.
waited=0
while [ ! -S /run/tailscale/tailscaled.sock ] \\
   && [ ! -S /var/run/tailscale/tailscaled.sock ] \\
   && [ ! -S /var/run/tailscaled.socket ]; do
    waited=$((waited + 1))
    if [ "$waited" -gt 60 ]; then
        echo "bothy: no tailscaled socket after 60s; starting anyway" >&2
        break
    fi
    sleep 1
done

set +e
"{target.repo}/bin/bothy" serve --reap
code=$?
set -e
{tail}'''


def render_launchd(target: Target) -> str:
    """A LaunchDaemon, not a LaunchAgent.

    A LaunchAgent needs a GUI login session, which an unattended Mac mini does
    not reliably have after a power cut. A daemon starts at boot without anyone
    logging in — which is the entire point of leaving one somewhere.
    """
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Generated by bothy install. No secrets here: this file is world-readable. -->
<plist version="1.0">
<dict>
    <key>Label</key>              <string>{target.label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>{target.wrapper_path}</string>
    </array>
    <key>UserName</key>           <string>{target.user}</string>
    <key>RunAtLoad</key>          <true/>
    <key>KeepAlive</key>
    <dict>
        <!-- Restart on a crash or a non-zero exit; a clean exit is deliberate
             and includes the wrapper's translation of exit 78. -->
        <key>SuccessfulExit</key> <false/>
    </dict>
    <!-- Back off between restarts so a failing start cannot spin. -->
    <key>ThrottleInterval</key>   <integer>20</integer>
    <key>StandardOutPath</key>    <string>{target.log_dir}/bothy.log</string>
    <key>StandardErrorPath</key>  <string>{target.log_dir}/bothy.log</string>
    <key>WorkingDirectory</key>   <string>{target.repo}</string>
    <key>ProcessType</key>        <string>Background</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>BOTHY_HOME</key>     <string>{target.home}</string>
        <key>PATH</key>           <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
'''


def render_systemd(target: Target) -> str:
    return f'''# Generated by bothy install. No secrets here.
[Unit]
Description=Bothy ({target.site})
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User={target.user}
WorkingDirectory={target.repo}
ExecStart=/bin/sh {target.wrapper_path}
Restart=always
RestartSec=10
# The other half of the exit-code contract: 78 means restarting cannot help, so
# systemd stops instead of burning the restart budget that exists for crashes.
RestartPreventExitStatus=78
TimeoutStopSec=60
KillMode=control-group
StandardOutput=append:{target.log_dir}/bothy.log
StandardError=append:{target.log_dir}/bothy.log
Environment=BOTHY_HOME={target.home}

[Install]
WantedBy=multi-user.target
'''


def render_newsyslog(target: Target) -> str:
    """launchd rotates nothing, so this is not optional on a Mac."""
    return (
        "# Generated by bothy install. launchd does no log rotation.\n"
        "# path                          owner:group  mode count size  when  flags\n"
        f"{target.log_dir}/bothy.log      {target.user}:wheel  644  7     10240 *     GJ\n"
    )


def render_serve_command(port: int) -> list[str]:
    """Expose the loopback listener to the tailnet, and to nothing else."""
    return ["tailscale", "serve", "--bg", f"https:{port}", "/", f"http://127.0.0.1:{port}"]


def already_installed(target: Target) -> bool:
    return target.unit_path.exists()


def activate(target: Target) -> list[str]:
    """The commands that load the service, for running or for printing."""
    if target.kind == LAUNCHD:
        return [
            f"launchctl bootout system/{target.label} 2>/dev/null || true",
            f"launchctl bootstrap system {target.unit_path}",
            f"launchctl enable system/{target.label}",
            f"launchctl kickstart -k system/{target.label}",
        ]
    return [
        "systemctl daemon-reload",
        f"systemctl enable {target.label}.service",
        f"systemctl restart {target.label}.service",
    ]


def deactivate(target: Target) -> list[str]:
    if target.kind == LAUNCHD:
        return [f"launchctl bootout system/{target.label} 2>/dev/null || true"]
    return [
        f"systemctl disable --now {target.label}.service 2>/dev/null || true",
        "systemctl daemon-reload",
    ]
