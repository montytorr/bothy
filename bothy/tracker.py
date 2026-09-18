"""Cairn as Bothy's memory and task ledger.

Bothy shells out to the ``cairn`` CLI rather than calling the HTTP API or the
MCP facade, and that is a deliberate choice with three reasons behind it.

The CLI carries an OFFLINE OUTBOX. When the server is unreachable, notes,
comments and checkpoints are queued to disk and replayed on the next successful
write, while ``add`` and ``claim`` deliberately fail fast — because an agent
handed a reference that does not exist yet, or told it holds a task it may not
have won, is worse off than one told plainly that the write failed. Reproducing
that judgement over raw HTTP would be duplicating a decision someone already
got right.

The CLI also maintains OWNERSHIP GENERATIONS, mirroring the server's version
numbers to disk so a checkpoint written during an outage can fail closed rather
than guess, and it writes a breadcrumb per accepted write that Cairn's own
session recorder uses to attribute work. All of that comes free.

And the MCP facade exposes thirteen of thirty-odd commands, omitting knowledge,
``next``, ``context`` and ``beat`` entirely — a strict subset with nothing to
offer a process that can simply run the binary.

IDENTITY IS SET EXPLICITLY. ``CAIRN_AGENT`` is always passed, never inferred:
on the reference host every runtime silently wrote as one identity for weeks
because detection guessed, and per-agent numbers were meaningless until someone
noticed.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Sequence

from . import clock

__all__ = ["CairnTracker", "CairnUnavailable"]


class CairnUnavailable(RuntimeError):
    """The CLI is missing or refused a call that mattered."""


class CairnTracker:
    """A TaskTracker backed by the Cairn CLI.

    Satisfies the reactor's ``TaskTracker`` protocol (find_open_for_contract,
    annotate, close) and adds the claim/checkpoint/release lifecycle Bothy needs
    around a run.
    """

    def __init__(
        self,
        *,
        agent: str = "bothy",
        binary: str = "cairn",
        project: str | None = None,
        timeout: float = 30.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self.agent = agent
        self.binary = binary
        self.project = project
        self.timeout = timeout
        self._env = env

    # ---- plumbing ------------------------------------------------------

    def _run(self, args: Sequence[str], *, stdin: str | None = None, check: bool = False) -> subprocess.CompletedProcess:
        env = dict(os.environ if self._env is None else self._env)
        env["CAIRN_AGENT"] = self.agent
        env.setdefault("CAIRN_PLATFORM", self.agent)
        try:
            result = subprocess.run(  # noqa: S603 - argv is constructed, never shell
                [self.binary, *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
            )
        except FileNotFoundError as exc:
            raise CairnUnavailable(f"{self.binary} is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise CairnUnavailable(f"{self.binary} {args[0]} timed out after {self.timeout}s") from exc
        if check and result.returncode != 0:
            raise CairnUnavailable(f"{self.binary} {' '.join(args)} failed: {result.stderr.strip()}")
        return result

    def available(self) -> bool:
        """Whether the CLI answers at all. Cheap enough for a health check."""
        try:
            return self._run(["--version"]).returncode == 0
        except CairnUnavailable:
            return False

    # ---- the run lifecycle ---------------------------------------------

    ALREADY_CLAIMED = 9

    def claim(self, ref: str) -> bool:
        """Take a task. False means somebody else already holds it.

        Exit code 9 is Cairn's specific "already claimed" and is not an error
        here: it is the answer. Bothy's own lane check should have caught this
        first, so a 9 means another agent entirely — which is exactly the case
        where refusing quietly and moving on is correct.
        """
        result = self._run(["claim", ref])
        if result.returncode == self.ALREADY_CLAIMED:
            return False
        if result.returncode != 0:
            raise CairnUnavailable(f"claim {ref} failed: {result.stderr.strip()}")
        return True

    def checkpoint(self, ref: str, summary: str) -> bool:
        """Record where a run got to. Queued locally if the server is away."""
        return self._run(["checkpoint", ref, "--summary", "-"], stdin=summary).returncode == 0

    def release(self, ref: str) -> bool:
        return self._run(["release", ref]).returncode == 0

    def beat(self, ref: str) -> bool:
        """Keep a claim alive during a long run."""
        return self._run(["beat", ref]).returncode == 0

    # ---- the reactor's TaskTracker protocol ----------------------------

    def find_open_for_contract(self, contract_id: str) -> list[str]:
        """Open tasks explicitly labelled with this contract.

        An explicit label, never a text search: a fuzzy match here would
        annotate — or on an approved closure, close — work belonging to
        something else entirely.
        """
        args = ["list", "--label", f"a2a-contract:{contract_id}", "--status", "doing"]
        if self.project:
            args += ["--project", self.project]
        result = self._run(args)
        if result.returncode != 0:
            return []
        refs: list[str] = []
        for line in result.stdout.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            first = line.split("\t", 1)[0].strip()
            if first and first != "ref" and "-" in first:
                refs.append(first)
        return refs

    def annotate(self, ref: str, note: str) -> bool:
        return self._run(["note", ref, "-", "--kind", "note"], stdin=note).returncode == 0

    def close(self, ref: str, resolution: str) -> bool:
        return self._run(["done", ref, "--resolution", "-"], stdin=resolution).returncode == 0

    # ---- what Bothy writes around a run --------------------------------

    def record_run(self, ref: str, *, run_id: str, outcome: str, detail: str) -> bool:
        """One note per finished run, stamped with the id that joins every layer."""
        body = (
            f"bothy run {run_id} — {outcome}\n"
            f"at {clock.iso()}\n\n"
            f"{detail}"
        )
        kind = "finding" if outcome != "completed" else "note"
        return self._run(["note", ref, "-", "--kind", kind], stdin=body).returncode == 0
