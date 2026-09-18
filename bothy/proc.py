"""Spawning workers so they can always be killed again.

A Codex app-server is not one process. The installed entry point is a Node
shim that execs a Rust binary, and some runs add a third code-mode host child.
Signalling the shim does not reliably reap the rest. That is how thirteen
orphaned app-servers, holding nearly a gigabyte, were found alive on the
reference host — and how a forensic post-mortem of one runaway agent lost 196
orphaned children and $1,193 of already-spent work with them.

So every worker is spawned into its OWN PROCESS GROUP and killed as a group.
The group is the unit of life, because it is the only unit that contains the
whole tree.

Two other rules earn their place here:

REFUSE TO START ON A DIRTY HOUSE. If a previous instance's children are still
alive, Bothy does not quietly add more beside them. systemd logs "Found
left-over process ... Ignoring", and ignoring is how five duplicate listeners
accumulated and fought over one session. A restart must REPLACE, never add.

COUNT FROM THE RECORD, NOT FROM pgrep. Matching process command lines catches
the investigator's own shell, which is how a process count came back inflated
during a real incident. Bothy knows the groups it started because it wrote them
down; that file is the authority.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

from . import clock

__all__ = ["Worker", "ProcessRegistry", "spawn_group", "kill_group", "group_alive", "group_members", "pid_alive"]


# --------------------------------------------------------------------------
# primitives


def pid_alive(pid: int) -> bool:
    """Whether a pid currently exists. Says nothing about WHICH process it is."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True  # exists, owned by someone else
        raise
    return True


def _proc_stat(pid: int) -> list[str] | None:
    """Fields of /proc/<pid>/stat from the state field onwards, or None.

    The comm field is parenthesised and may itself contain spaces or brackets,
    so the split is taken after the FINAL ')' rather than from the left. With
    that done, index 0 is state, index 2 is the process group id and index 19
    is the start time.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    tail = raw.rpartition(")")[2].split()
    return tail if len(tail) >= 20 else None


def group_members(pgid: int) -> list[int]:
    """Live pids in a process group, excluding zombies.

    A zombie is a process that has exited and is waiting to be reaped. It still
    owns a pid, so ``killpg(pgid, 0)`` reports the group as alive and a
    supervisor that trusts that will wait out its whole grace period on a
    corpse — observed while building this, with an unreaped group leader
    reported "unkillable" after its entire tree had in fact exited.

    Reading /proc costs a directory scan and answers honestly, which is the
    same trade as counting from a cgroup rather than from pgrep.
    """
    if pgid <= 0:
        return []
    members: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        fields = _proc_stat(int(entry))
        if fields is None:
            continue
        if fields[0] == "Z":  # exited, not yet reaped: dead for our purposes
            continue
        if fields[2] == str(pgid):
            members.append(int(entry))
    return sorted(members)


def group_alive(pgid: int) -> bool:
    """Whether any non-zombie member of a process group is still running."""
    return bool(group_members(pgid))


def _start_time(pid: int) -> str | None:
    """Kernel start time for a pid, as a fingerprint against pid reuse.

    A pid alone is not an identity: the kernel recycles them, and a reaper that
    trusts a bare pid will eventually kill something innocent. Field 22 of
    /proc/<pid>/stat is the process start time in clock ticks since boot, which
    together with the pid is unique for the life of the boot.
    """
    fields = _proc_stat(pid)
    return fields[19] if fields else None


def spawn_group(
    argv: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    stdin: int | None = subprocess.PIPE,
    stdout: int | None = subprocess.PIPE,
    stderr: int | None = subprocess.PIPE,
) -> subprocess.Popen:
    """Start a command as the leader of a brand new process group.

    ``start_new_session`` makes the child a session and group leader, so its
    pgid equals its pid and every descendant inherits the group unless it goes
    out of its way to leave. Detaching from the controlling terminal also stops
    a child inheriting a TTY that may later be deleted — an orphaned Codex once
    held an exclusive thread-writer lock with its stdio pointing at a deleted
    /dev/pts, which no amount of retrying could clear.
    """
    return subprocess.Popen(  # noqa: S603 - argv is constructed, never shell
        list(argv),
        env=env,
        cwd=str(cwd) if cwd is not None else None,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
        close_fds=True,
    )


def kill_group(pgid: int, *, grace_seconds: float = 10.0, poll_seconds: float = 0.1) -> str:
    """Terminate a whole process group, escalating only if it will not go.

    SIGTERM first so a worker can close its files and settle its budget, then
    SIGKILL if the grace period passes. Returns what it took: "already-gone",
    "terminated" or "killed".

    The grace period is measured on the monotonic clock, so an NTP step during
    shutdown cannot cut it short or stretch it indefinitely.
    """
    if not group_alive(pgid):
        return "already-gone"

    with contextlib.suppress(OSError):
        os.killpg(pgid, signal.SIGTERM)

    deadline = clock.monotonic() + grace_seconds
    while clock.monotonic() < deadline:
        if not group_alive(pgid):
            return "terminated"
        time.sleep(poll_seconds)

    with contextlib.suppress(OSError):
        os.killpg(pgid, signal.SIGKILL)

    deadline = clock.monotonic() + grace_seconds
    while clock.monotonic() < deadline:
        if not group_alive(pgid):
            return "killed"
        time.sleep(poll_seconds)
    return "unkillable"


# --------------------------------------------------------------------------
# the record of what we started


@dataclasses.dataclass
class Worker:
    """One spawned worker, described well enough to identify it after a crash.

    ``start_ticks`` is the kernel start time of the leader. A pid on its own is
    not an identity — pids are recycled, and a reaper that trusts a bare pid
    will eventually kill an innocent process that happens to have inherited the
    number. Together they are unique for the life of the boot, and ``boot_id``
    makes the record safe to read after a reboot as well.
    """

    run_id: str
    pid: int
    pgid: int
    argv: list[str]
    started_at: str
    start_ticks: str | None = None
    boot_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Worker":
        return cls(**raw)

    def still_ours(self) -> bool:
        """Whether the live group is the one we started, not a pid reuse.

        Returns False after a reboot: the recorded boot id no longer matches, so
        whatever holds that pid now is somebody else's and must not be signalled.
        """
        if self.boot_id is not None and self.boot_id != _boot_id():
            return False
        if not group_alive(self.pgid):
            return False
        if self.start_ticks is None:
            return True
        return _start_time(self.pid) == self.start_ticks


def _boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return None


class ProcessRegistry:
    """Durable list of worker process groups this harness started.

    Written before the worker is useful and removed after it is reaped, so the
    file always over-states rather than under-states what is running. Over-
    stating costs a liveness check; under-stating costs an orphan nobody knows
    to kill.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, prefix=self.path.name, suffix=".tmp", delete=False
        )
        try:
            with handle:
                json.dump(data, handle, sort_keys=True, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(handle.name)
            raise

    def register(self, run_id: str, popen: subprocess.Popen, argv: Sequence[str]) -> Worker:
        worker = Worker(
            run_id=run_id,
            pid=popen.pid,
            pgid=popen.pid,  # start_new_session makes the child its own group leader
            argv=list(argv),
            started_at=clock.iso(),
            start_ticks=_start_time(popen.pid),
            boot_id=_boot_id(),
        )
        data = self._load()
        data[run_id] = worker.as_dict()
        self._save(data)
        return worker

    def unregister(self, run_id: str) -> None:
        data = self._load()
        if data.pop(run_id, None) is not None:
            self._save(data)

    def workers(self) -> list[Worker]:
        return [Worker.from_dict(raw) for raw in self._load().values()]

    def survivors(self) -> list[Worker]:
        """Recorded workers whose group is still alive and still genuinely ours."""
        return [worker for worker in self.workers() if worker.still_ours()]

    def reap(self, *, grace_seconds: float = 10.0) -> list[tuple[Worker, str]]:
        """Kill every surviving worker group and forget every recorded worker.

        Called at startup. A harness that finds its predecessor's children still
        running must replace them, not add beside them — the alternative is the
        duplicate-listener failure where several instances fight over one
        session and the oldest silently wins.
        """
        outcomes: list[tuple[Worker, str]] = []
        for worker in self.workers():
            if worker.still_ours():
                outcomes.append((worker, kill_group(worker.pgid, grace_seconds=grace_seconds)))
            else:
                outcomes.append((worker, "stale-record"))
        self._save({})
        return outcomes
