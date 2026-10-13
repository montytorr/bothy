"""Live human replies: ephemeral routing and announced, identity-checked controls."""
from __future__ import annotations

import collections
import contextlib
import dataclasses
import fcntl
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from . import clock, proc
from .trace import span_fields

INTERRUPT_ID = re.compile(r"int_[0-9a-f]{32}\Z")
_REPLY_SLOTS = threading.BoundedSemaphore(2)


def valid_id(value: str) -> bool:
    return bool(INTERRUPT_ID.fullmatch(value))


@contextlib.contextmanager
def control_transaction(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".interrupts.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def write_control(directory: Path, name: str, body: dict[str, Any]) -> None:
    """Caller holds control_transaction; replace atomically, fsync before ack."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, prefix="interrupt-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(body, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / name)
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def pending_owner(run_id: str) -> dict[str, Any]:
    return proc.Worker(run_id, os.getpid(), os.getpgrp(), ["bothy"], clock.iso(),
                       proc._start_time(os.getpid()), proc.boot_id()).as_dict()


def live_pending(directory: Path, interrupt_id: str) -> bool:
    """An announced marker, before deadline, held by the same live process."""
    if not valid_id(interrupt_id):
        return False
    try:
        body = json.loads((directory / f"{interrupt_id}.pending").read_text(encoding="utf-8"))
        owner = proc.Worker.from_dict(body["owner"])
        return (clock.monotonic() < float(body["deadline"]) and owner.start_ticks is not None
                and proc.pid_alive(owner.pid) and owner.still_ours())
    except (OSError, ValueError, KeyError, TypeError):
        return False


@dataclasses.dataclass
class Entry:
    id: str
    deadline: float
    run_id: str
    subject: str
    origin: dict[str, Any]
    answer: Callable[[str, str], bool]
    refs: set[str] = dataclasses.field(default_factory=set)


class InterruptBoard:
    def __init__(self, audit: Any, notify: Callable[[dict[str, Any], str], Any] | None = None) -> None:
        self.audit = audit
        self.notify = notify
        self._lock = threading.RLock()
        self._entries: dict[str, Entry] = {}
        self._closed: collections.OrderedDict[str, Entry] = collections.OrderedDict()
        self._deliveries: collections.OrderedDict[str, None] = collections.OrderedDict()

    def register(self, entry: Entry) -> None:
        with self._lock:
            self._entries[entry.id] = entry

    def references(self, interrupt_id: str, refs: list[str]) -> None:
        with self._lock:
            if interrupt_id in self._entries:
                self._entries[interrupt_id].refs.update(refs)

    def remove(self, interrupt_id: str) -> None:
        with self._lock:
            entry = self._entries.pop(interrupt_id, None)
            if entry is not None:
                self._closed[interrupt_id] = dataclasses.replace(entry, answer=lambda text, actor: False)
                while len(self._closed) > 1024:
                    self._closed.popitem(last=False)

    def _reply(self, source: str, channel: str, reference: str, text: str) -> None:
        if self.notify is None or not _REPLY_SLOTS.acquire(blocking=False):
            return
        origin = {source: {"channel": channel, "thread_ts" if source == "slack" else "message_id": reference}}
        def send():
            try:
                self.notify(origin, text)
            except Exception:  # noqa: BLE001 - replies cannot stall inbound or supervision
                pass
            finally:
                _REPLY_SLOTS.release()
        try:
            threading.Thread(target=send, daemon=True).start()
        except RuntimeError:
            _REPLY_SLOTS.release()

    def _record(self, action: str, entry: Entry | None, actor: str, delivery: str, channel: str, **data: Any) -> None:
        fields = {}
        if entry is not None:
            fields = {"run_id": entry.run_id,
                      "span": span_fields(entry.run_id, f"{entry.id}:{action}:{delivery}", parent="turn:1",
                                          start=clock.iso(), end=clock.iso(), status="refused")}
            data.setdefault("interrupt_id", entry.id)
        self.audit.append(kind="interrupt", action=action, status="refused", actor=actor,
                          subject=entry.subject if entry else channel, data=data, **fields)

    def receive(self, *, source: str, channel: str, reference: str, text: str,
                actor: str, allowed: bool, delivery: str) -> bool:
        parts = text.split(maxsplit=2)
        explicit = len(parts) >= 2 and parts[0].lower() == "answer" and valid_id(parts[1])
        def matches(entry: Entry) -> bool:
            if explicit:
                return parts[1] == entry.id
            where = entry.origin.get(source) or {}
            return bool(reference and str(where.get("channel") or "") == channel and (
                reference in entry.refs or (source == "slack" and reference == where.get("thread_ts"))))
        with self._lock:
            if allowed and delivery in self._deliveries:
                return True
            candidates = [entry for entry in self._entries.values() if matches(entry) and clock.monotonic() < entry.deadline]
            closed = next((entry for entry in [*self._entries.values(), *self._closed.values()]
                           if matches(entry)), None)
        # All audit, outbound notifications and client callbacks run without the board lock.
        if not candidates:
            if not explicit and closed is None:
                return False
            self._record("late" if allowed else "ignored", closed, actor, delivery, channel,
                         interrupt_id=parts[1] if explicit else closed.id, reason="hold is not live")
            if allowed:
                self._reply(source, channel, reference, "That question has already closed; your answer was not delivered.")
            return True
        matched = candidates[0]
        if not allowed:
            self._record("ignored", matched, actor, delivery, channel, reason="speaker is not on the allowlist")
            return True
        if not explicit and len(candidates) > 1:
            identifiers = [entry.id for entry in candidates]
            self._record("ambiguous", matched, actor, delivery, channel, interrupt_ids=identifiers,
                         reason="more than one live request in this thread")
            self._reply(source, channel, reference,
                        "Several requests are waiting here: " + ", ".join(identifiers) +
                        ". Reply with answer <int_id> <text> to choose one.")
            return True
        reply = parts[2] if explicit and len(parts) == 3 else ("" if explicit else text)
        if matched.answer(reply, actor):
            with self._lock:
                self._deliveries[delivery] = None
                while len(self._deliveries) > 1024:
                    self._deliveries.popitem(last=False)
        else:
            self._record("late" if clock.monotonic() >= matched.deadline else "failed", matched, actor, delivery, channel,
                         reason="question closed or answer could not be recorded")
            self._reply(source, channel, reference,
                        "That question has closed or the answer could not be recorded; your answer was not delivered.")
        return True
