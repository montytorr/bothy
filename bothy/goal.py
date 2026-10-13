"""Verification of a scheduled run's effect, with bounded, untrusted evidence."""

from __future__ import annotations

import dataclasses
import math
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import proc

EVIDENCE_BYTES = 4096
MAX_TIMEOUT_SECONDS = 600


def bounded(text: str) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= EVIDENCE_BYTES:
        return text
    suffix = "\n[truncated]"
    return raw[:EVIDENCE_BYTES - len(suffix)].decode("utf-8", errors="ignore") + suffix


def validate_definition(validate: Any, max_attempts: Any) -> None:
    """Refuse malformed goals when the job is written."""
    if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
        raise ValueError("max_attempts must be an integer from 1 to 5")
    if validate is None:
        if max_attempts != 1:
            raise ValueError("max_attempts greater than 1 requires validate")
        return
    if not isinstance(validate, dict):
        raise ValueError("validate must be a command or output_regex object")
    if "command" in validate and set(validate) <= {"command", "timeout_seconds"}:
        argv = validate["command"]
        if not isinstance(argv, list) or not argv or not argv[0] or any(
            not isinstance(arg, str) or "\x00" in arg for arg in argv
        ):
            raise ValueError("validate command must be a nonempty list of argv strings")
        timeout = validate.get("timeout_seconds", 120)
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not 0 < timeout <= MAX_TIMEOUT_SECONDS or not math.isfinite(timeout)):
            raise ValueError(f"validate timeout_seconds must be finite, positive and at most {MAX_TIMEOUT_SECONDS}")
    elif set(validate) == {"output_regex"}:
        pattern = validate["output_regex"]
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("validate output_regex must be a nonempty string")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"validate output_regex is invalid: {exc}") from exc
    else:
        raise ValueError("validate needs exactly one of command or output_regex")


@dataclasses.dataclass(frozen=True)
class Verdict:
    ok: bool
    command: list[str] | str | None
    evidence: str


def check(goal: dict[str, Any], result: Any, workspace: Path) -> Verdict:
    """A run must complete and its check must find evidence before it passes."""
    validate = goal.get("validate")
    try:
        validate_definition(validate, goal.get("max_attempts", 1))
        if validate is None:
            raise ValueError("goal is missing validate")
    except ValueError as exc:
        return Verdict(False, None, bounded(str(exc)))
    command = validate.get("command") or f"output_regex: {validate['output_regex']}"
    if result.status != "completed":
        reason = result.error or (result.refusal or {}).get("reason") or result.status
        stop_reason = getattr(result, "stop_reason", None)
        stopped = f" (stop_reason: {stop_reason})" if stop_reason else ""
        return Verdict(False, command, bounded(f"Run did not complete{stopped}: {reason}"))

    if "output_regex" in validate:
        output = "\n".join(result.messages)
        if not output.strip():
            return Verdict(False, command, "Run produced no final output")
        ok = re.search(validate["output_regex"], output) is not None
        return Verdict(ok, command, bounded(f"Regex {'matched' if ok else 'did not match'}:\n{output}"))

    # A file keeps arbitrary check output out of memory; only bounded evidence
    # is read back. The check gets no interactive stdin and never a shell.
    with tempfile.TemporaryFile() as output:
        try:
            child = proc.spawn_group(command, cwd=workspace, stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=subprocess.STDOUT)
        except OSError as exc:
            return Verdict(False, command, bounded(f"Check could not start: {exc}"))
        timed_out = False
        try:
            try:
                child.wait(timeout=validate.get("timeout_seconds", 120))
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                # Even a successful leader can leave descendants behind.
                proc.kill_group(child.pid, grace_seconds=0)
                child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return Verdict(False, command, "Check process group could not be reaped after its deadline")
        output.seek(0)
        evidence = output.read(EVIDENCE_BYTES + 1).decode("utf-8", errors="replace")
    if timed_out:
        return Verdict(False, command, bounded(f"Check timed out:\n{evidence}"))
    if not evidence.strip():
        return Verdict(False, command, f"Check exited {child.returncode} with empty output")
    return Verdict(child.returncode == 0, command, bounded(f"Check exited {child.returncode}:\n{evidence}"))
