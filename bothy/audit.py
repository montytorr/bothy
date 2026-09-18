"""The record of what Bothy did, in one file, joinable by run id.

Bothy runs unattended on somebody else's machine. The question that matters
months later is "what did it actually do, and on whose instruction", and the
honest answer needs three properties the systems Bothy learns from lack.

ONE JOIN KEY. Every record carries ``run_id``, so one grep reconstructs a run
across wake, admission, worker, budget and outcome. Elsewhere those live under
four different keys with no join.

TAMPER EVIDENCE. Records are hash-chained: each one commits to its predecessor,
so a later edit or deletion breaks the chain at that point and ``verify`` says
where. This is evidence of tampering, not prevention — anyone who can write the
file can rewrite the whole chain. It is worth having anyway, because the
realistic failure is a well-meaning edit or a truncating crash, not an attacker.

WRITES FAIL LOUDLY. ``append`` raises. Observability that fails open is how a
system spends five months writing a heartbeat nobody read; an audit log that
silently stops is worse than one that was never claimed. Bothy's policy on a
raise is to stop admitting new work, alert, and let in-flight runs finish — an
unauditable harness at a client site should get quieter, not carry on.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Iterator

from . import clock

__all__ = ["AuditLog", "AuditError", "ChainBreak"]

GENESIS = "0" * 64


class AuditError(RuntimeError):
    """The audit record could not be written."""


class ChainBreak(RuntimeError):
    """The chain does not verify, and this is where it stops."""

    def __init__(self, message: str, *, sequence: int, path: str) -> None:
        super().__init__(message)
        self.sequence = sequence
        self.path = path


def _canonical(record: dict[str, Any]) -> bytes:
    """Bytes a hash commits to: every field except the hash itself.

    Sorted keys and no insignificant whitespace, so the same record hashes the
    same way on any Python version and in any field order.
    """
    body = {key: value for key, value in record.items() if key != "hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(record: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(record)).hexdigest()


class AuditLog:
    """An append-only, hash-chained JSONL log.

    Not SQLite on purpose. An operator at a client site should be able to read
    the audit trail with ``cat`` and mail it to us, with no tooling and no lock
    to contend for. The volume is one record per interesting decision, not one
    per tool call, so a text file is the right size of answer.
    """

    def __init__(self, path: str | os.PathLike[str], *, max_bytes: int = 32 * 1024 * 1024) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._tip: tuple[int, str] | None = None

    # ---- reading -------------------------------------------------------

    def records(self) -> Iterator[dict[str, Any]]:
        """Every well-formed record, oldest first. A bad line is skipped.

        A truncated final line costs one record rather than the file; ``verify``
        is the thing that will notice and say so.
        """
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record

    def tip(self) -> tuple[int, str]:
        """The last sequence number and hash, for chaining the next record."""
        if self._tip is not None:
            return self._tip
        sequence, previous = 0, GENESIS
        for record in self.records():
            sequence = int(record.get("seq", sequence))
            previous = str(record.get("hash", previous))
        self._tip = (sequence, previous)
        return self._tip

    def verify(self) -> int:
        """Walk the chain. Return the count verified, or raise ChainBreak.

        Checks three things per record: that the sequence increments by one,
        that ``prev`` matches the predecessor's hash, and that the hash is the
        digest of the record's own contents.
        """
        expected_seq, previous = 1, GENESIS
        count = 0
        for record in self.records():
            seq = record.get("seq")
            if seq != expected_seq:
                raise ChainBreak(
                    f"sequence jumped: expected {expected_seq}, found {seq!r}",
                    sequence=expected_seq,
                    path=str(self.path),
                )
            if record.get("prev") != previous:
                raise ChainBreak(
                    f"record {seq} does not follow its predecessor",
                    sequence=int(seq),
                    path=str(self.path),
                )
            recomputed = _digest(record)
            if record.get("hash") != recomputed:
                raise ChainBreak(
                    f"record {seq} has been altered since it was written",
                    sequence=int(seq),
                    path=str(self.path),
                )
            previous = recomputed
            expected_seq += 1
            count += 1
        return count

    # ---- writing -------------------------------------------------------

    def append(
        self,
        *,
        kind: str,
        action: str,
        status: str = "ok",
        run_id: str | None = None,
        subject: str | None = None,
        actor: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write one record and return it. Raises AuditError if it cannot.

        ``kind`` groups records (wake, admission, run, budget, alert). ``action``
        is the specific thing (received, refused, started, settled). ``status``
        is ok, failed or refused — refusals are first-class, because "did not
        start, and why" is the question an operator actually asks.
        """
        with self._lock:
            sequence, previous = self.tip()
            record: dict[str, Any] = {
                "seq": sequence + 1,
                "ts": clock.iso(),
                "kind": kind,
                "action": action,
                "status": status,
                "run_id": run_id,
                "subject": subject,
                "actor": actor,
                "data": data or {},
                "prev": previous,
            }
            record["hash"] = _digest(record)
            line = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # Opened per append and fsynced: the cost is irrelevant at this
                # volume, and a record that is only in the page cache when the
                # box loses power is not a record.
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                self._tip = None  # our idea of the tip is no longer trustworthy
                raise AuditError(f"could not append to {self.path}: {exc}") from exc
            self._tip = (record["seq"], record["hash"])
            return record
