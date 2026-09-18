"""Starting once, dying honestly, and telling the supervisor what to do next.

Three jobs, each answering a failure that is embarrassing to discover in
production.

ONE INSTANCE. A second Bothy on the same state directory would hand out the
same worker slots and the same budget twice. The lock is an advisory flock held
for the life of the process, so a crash releases it without leaving anything to
clean up — and a clean refusal names the pid that holds it, because "address
already in use" tells an operator nothing about who to talk to.

A RESTART REPLACES, IT DOES NOT ADD. If a previous instance's workers are still
running, Bothy refuses to start beside them. systemd's equivalent logs "Found
left-over process ... Ignoring", and ignoring is exactly how five duplicate
listeners once accumulated and fought over one session until the oldest
silently won.

THE EXIT CODE IS A MESSAGE TO THE SUPERVISOR. Two values, borrowed from
sysexits and independently used by the reference deployment's own service unit:

    75  EX_TEMPFAIL  something transient — restart me
    78  EX_CONFIG    something I cannot fix by restarting — stop

Without that distinction a supervisor with ``Restart=always`` will loop forever
on a typo in a config file, burning the restart budget that exists to protect
against real crashes.

And a fourth, quieter job: the ledger notices that the LAST run died badly. A
sentinel whose recorded phase is still "running" at the next boot is proof of an
unclean death, and it is recorded with whatever the kernel still remembers about
why — because "it restarted and I do not know why" is the most expensive kind of
incident to investigate later.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from . import clock

__all__ = ["EX_TEMPFAIL", "EX_CONFIG", "AlreadyRunning", "InstanceLock", "LifecycleLedger", "UncleanDeath"]

EX_TEMPFAIL = 75
EX_CONFIG = 78


class AlreadyRunning(RuntimeError):
    """Another Bothy holds the state directory."""

    def __init__(self, pid: int | None, path: Path) -> None:
        holder = f"pid {pid}" if pid else "another process"
        super().__init__(f"{holder} already holds {path}")
        self.pid = pid
        self.path = path


class InstanceLock:
    """An advisory lock on the state directory, held for the process lifetime."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EAGAIN, errno.EACCES}:
                handle.seek(0)
                raw = handle.read().strip()
                handle.close()
                raise AlreadyRunning(int(raw) if raw.isdigit() else None, self.path) from exc
            handle.close()
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        return self

    def release(self) -> None:
        if self._handle is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(self._handle, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            self._handle.close()
        self._handle = None

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self.release()


@dataclasses.dataclass
class UncleanDeath:
    """What the previous run left behind, and whatever explains it."""

    started_at: str
    last_seen_at: str
    pid: int
    boot_id: str | None
    same_boot: bool
    evidence: dict[str, Any]

    def summary(self) -> str:
        cause = "the machine rebooted" if not self.same_boot else "the process died without shutting down"
        oom = " (memory pressure is the likely cause)" if self.evidence.get("suspected_oom") else ""
        return (
            f"previous run (pid {self.pid}, started {self.started_at}) never recorded a clean stop; "
            f"{cause}{oom}. Last sign of life {self.last_seen_at}."
        )


class LifecycleLedger:
    """A sentinel that makes an unclean death visible at the next start.

    Written at start, touched while running, and cleared on a clean stop. If it
    is still marked ``running`` when Bothy next starts, the previous process did
    not get to say goodbye — which is the only way to distinguish a crash, an
    OOM kill and a power cut from an ordinary restart after the fact.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, prefix=self.path.name, suffix=".tmp", delete=False
        )
        try:
            with handle:
                json.dump(state, handle, sort_keys=True, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(handle.name)
            raise

    @staticmethod
    def _boot_id() -> str | None:
        try:
            return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        except OSError:
            return None

    @staticmethod
    def _memory_evidence() -> dict[str, Any]:
        """Whatever the kernel still knows that might explain a sudden death."""
        evidence: dict[str, Any] = {}
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith(("MemAvailable:", "MemTotal:", "SwapFree:")):
                    key, _, value = line.partition(":")
                    evidence[key.strip()] = value.strip()
        except OSError:
            pass
        try:
            pressure = Path("/proc/pressure/memory").read_text(encoding="utf-8").strip()
            evidence["pressure_memory"] = pressure.splitlines()[0] if pressure else ""
            # CPU pressure is here too because the one host-wide stall in the
            # source system was CPU at 41% PSI with 52 of 62 GiB free — free
            # memory looked perfect throughout.
            cpu = Path("/proc/pressure/cpu").read_text(encoding="utf-8").strip()
            evidence["pressure_cpu"] = cpu.splitlines()[0] if cpu else ""
        except OSError:
            pass
        return evidence

    def detect_unclean(self) -> UncleanDeath | None:
        """Report whether the PREVIOUS run ended badly. Writes nothing.

        Read-only and separate from ``mark_running`` on purpose. An earlier
        version did both at once, so a start that was correctly REFUSED — for a
        missing secret, or because a predecessor's workers were still alive —
        left the ledger marked running and the next start reported a phantom
        unclean death. A refusal is the system working; it must not manufacture
        an incident, because an alert channel that cries wolf gets muted and
        then the real one is missed.
        """
        previous = self._read()
        if previous.get("phase") != "running":
            return None
        boot = previous.get("boot_id")
        return UncleanDeath(
            started_at=str(previous.get("started_at", "unknown")),
            last_seen_at=str(previous.get("last_seen_at", previous.get("started_at", "unknown"))),
            pid=int(previous.get("pid", 0)),
            boot_id=boot,
            same_boot=(boot == self._boot_id()),
            evidence=self._memory_evidence(),
        )

    def mark_running(self) -> None:
        """Claim the ledger. Called only once the daemon is committed to running."""
        now = clock.iso()
        self._write({
            "phase": "running",
            "pid": os.getpid(),
            "boot_id": self._boot_id(),
            "started_at": now,
            "last_seen_at": now,
        })

    def open_run(self) -> UncleanDeath | None:
        """Detect, then claim. Convenience for callers with nothing to refuse on."""
        unclean = self.detect_unclean()
        self.mark_running()
        return unclean

    def beat(self) -> None:
        """Record a sign of life, so an unclean death has a time attached."""
        state = self._read()
        if state.get("phase") != "running":
            return
        state["last_seen_at"] = clock.iso()
        with contextlib.suppress(OSError):
            self._write(state)

    def close_run(self, *, reason: str = "stopped") -> None:
        """Say goodbye properly, so the next start knows this was deliberate."""
        state = self._read()
        state.update({"phase": "stopped", "reason": reason, "stopped_at": clock.iso()})
        with contextlib.suppress(OSError):
            self._write(state)
