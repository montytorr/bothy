"""Sweeping up, on a schedule, so nothing grows without a bound.

Written the same day as the writers, deliberately. Retention added later is
retention added after the first incident, and on a small client machine the
first incident is a full disk at 3am — which takes the harness down along with
whatever else shares the volume.

The sizes here are not arbitrary. On the reference host, Codex's own state
directory reached 600 MB with a SINGLE session rollout of 275 MB, unbounded and
append-only with no rotation of its own. Bothy gives every worker a disposable
home precisely so that growth belongs to a run and dies with it — this module
is the backstop for the runs that did not get to clean up after themselves.

Everything here is idempotent and safe to run concurrently with live work. It
never deletes something a running worker owns: the process registry is consulted
first, and anything still claimed is left alone and reported rather than
removed.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import fcntl
import shutil
import time
from pathlib import Path
from typing import Any

from . import clock, memory, memory_index
from .audit import AuditError, AuditLog
from .budget import BudgetLedger
from .config import Config
from .proc import ProcessRegistry
from .vendor.a2a_reactor.queue import read_queue, write_queue

__all__ = ["RetentionPolicy", "SweepReport", "sweep"]


@dataclasses.dataclass(frozen=True)
class RetentionPolicy:
    """How long things live. Defaults must be right on a small unattended box."""

    audit_max_bytes: int = 32 * 1024 * 1024
    audit_keep_segments: int = 8
    wake_keep_days: float = 14.0
    wake_keep_entries: int = 5_000
    home_orphan_minutes: float = 60.0
    log_keep_days: float = 14.0


@dataclasses.dataclass
class SweepReport:
    """What the janitor did, for the audit log and for ``bothy status``."""

    audit_rotated: str | None = None
    wakes_dropped: int = 0
    wakes_remaining: int = 0
    homes_removed: list[str] = dataclasses.field(default_factory=list)
    homes_kept_live: list[str] = dataclasses.field(default_factory=list)
    reservations_expired: int = 0
    memory_preimages_removed: int = 0
    memory_days_removed: int = 0
    memory_index_rows_pruned: int = 0
    bytes_freed: int = 0
    errors: list[str] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def did_anything(self) -> bool:
        return bool(
            self.audit_rotated
            or self.wakes_dropped
            or self.homes_removed
            or self.reservations_expired
            or self.memory_preimages_removed
            or self.memory_days_removed
            or self.errors
        )

    def summary(self) -> str:
        if not self.did_anything():
            return "nothing to sweep"
        parts = []
        if self.audit_rotated:
            parts.append(f"rotated audit to {self.audit_rotated}")
        if self.wakes_dropped:
            parts.append(f"dropped {self.wakes_dropped} settled wake(s)")
        if self.homes_removed:
            parts.append(f"removed {len(self.homes_removed)} orphaned worker home(s)")
        if self.reservations_expired:
            parts.append(f"expired {self.reservations_expired} stale reservation(s)")
        if self.memory_preimages_removed:
            parts.append(f"removed {self.memory_preimages_removed} memory preimage(s)")
        if self.memory_days_removed:
            parts.append(f"removed {self.memory_days_removed} daily memory file(s)")
        if self.bytes_freed:
            parts.append(f"freed {self.bytes_freed // 1024} KiB")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return ", ".join(parts)


def _directory_size(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def sweep(config: Config, policy: RetentionPolicy | None = None) -> SweepReport:
    """Run every retention job once. Safe while work is in flight."""
    policy = policy or RetentionPolicy()
    report = SweepReport()

    # --- the audit log, rotated with its chain intact ---
    audit = AuditLog(config.audit_path, max_bytes=policy.audit_max_bytes)
    try:
        if audit.should_rotate():
            archived = audit.rotate(keep=policy.audit_keep_segments)
            report.audit_rotated = archived.name if archived else None
    except OSError as exc:
        report.errors.append(f"audit rotation: {exc}")

    # --- settled wakes, kept long enough to answer "did you get my webhook" ---
    queue_path = config.wake_dir / "wakes.jsonl"
    try:
        if queue_path.exists():
            events = read_queue(queue_path)
            cutoff = policy.wake_keep_days * 86400
            kept: list[dict[str, Any]] = []
            for event in events:
                if not event.get("processed"):
                    kept.append(event)  # unfinished work is never swept
                    continue
                try:
                    age = clock.age_seconds(str(event.get("received_at")))
                except (ValueError, TypeError):
                    kept.append(event)
                    continue
                if age < cutoff:
                    kept.append(event)
            if len(kept) > policy.wake_keep_entries:
                kept = kept[-policy.wake_keep_entries:]
            report.wakes_dropped = len(events) - len(kept)
            report.wakes_remaining = len(kept)
            if report.wakes_dropped:
                write_queue(queue_path, kept)
    except OSError as exc:
        report.errors.append(f"wake queue: {exc}")

    # --- worker homes whose run is gone ---
    registry = ProcessRegistry(config.registry_path)
    live = {worker.run_id for worker in registry.survivors()}
    try:
        if config.homes_dir.exists():
            for home in config.homes_dir.iterdir():
                if not home.is_dir() or home.name == "probe":
                    continue
                if home.name in live:
                    report.homes_kept_live.append(home.name)
                    continue
                try:
                    idle_minutes = (time.time() - home.stat().st_mtime) / 60.0
                except OSError:
                    continue
                if idle_minutes < policy.home_orphan_minutes:
                    # Young enough that a run may still be starting up. Removing
                    # a home out from under a live worker is far worse than
                    # leaving a directory for one more sweep.
                    continue
                size = _directory_size(home)
                shutil.rmtree(home, ignore_errors=True)
                report.homes_removed.append(home.name)
                report.bytes_freed += size
    except OSError as exc:
        report.errors.append(f"worker homes: {exc}")

    # --- budget held by runs that are not coming back ---
    try:
        ledger = BudgetLedger(config.budget_path, config.budget)
        report.reservations_expired = len(ledger.expire_stale())
    except OSError as exc:
        report.errors.append(f"budget: {exc}")

    # --- daily memory, by the date in the same timezone as the writer ---
    cutoff = memory.local_now(config.timezone).date() - dt.timedelta(days=config.memory_retention_days)
    profiles = set(config.profiles)
    if config.memory_dir.is_dir():
        profiles.update(path.name for path in config.memory_dir.iterdir() if path.is_dir())
    for profile in sorted(profiles):
        if memory.PROFILE_NAME.fullmatch(profile) is None:
            continue
        try:
            for file in ("memory", "user"):
                target = memory.resolve_file(config.memory_dir, profile, file)
                if not target.parent.is_dir():
                    continue
                with target.with_suffix(".lock").open("a") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                    expired = memory.preimages(config.memory_dir, profile, file)[:-14]
                    if not expired:
                        continue
                    size = sum(path.stat().st_size for path in expired)
                    audit.append(kind="memory", action="preimage_retention", status="pending",
                                 data={"profile": profile, "file": file,
                                       "files_removed": len(expired), "bytes": size})
                    for path in expired:
                        path.unlink()
                        report.memory_preimages_removed += 1
                    report.bytes_freed += size
        except (OSError, AuditError, memory.MemoryError):
            report.errors.append("memory preimages: could not sweep profile")
        removed = []
        try:
            daily = memory.resolve_path(config.memory_dir, profile, "daily")
            if not daily.is_dir():
                continue
            for candidate in sorted(daily.glob("*.md")):
                try:
                    path = memory.day_path(config.memory_dir, profile, candidate.stem)
                except memory.MemoryError:
                    continue
                if dt.date.fromisoformat(path.stem) >= cutoff:
                    continue
                lock_path = memory.resolve_path(config.memory_dir, profile, "daily/.lock")
                with lock_path.open("a") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                    if not path.is_file():
                        continue
                    size = path.stat().st_size
                    # Fail closed if intent cannot be audited before deletion.
                    audit.append(kind="memory", action="retention_delete", status="pending",
                                 data={"profile": profile, "date": path.stem, "files": 1, "bytes": size})
                    path.unlink()
                    removed.append(f"daily/{path.name}")
                    report.memory_days_removed += 1
                    report.bytes_freed += size
            if removed:
                pruned = memory_index.prune(config.state_dir, profile, removed)
                report.memory_index_rows_pruned += pruned
                audit.append(kind="memory", action="retention_sweep",
                             data={"profile": profile, "files_removed": len(removed), "index_rows_pruned": pruned})
        except (OSError, memory.MemoryError):
            report.errors.append("daily memory: could not sweep profile")

    return report
