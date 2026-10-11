"""Drove, as something Bothy starts and listens to. Not something it reasons about.

Drove is a planner and its workers in herdr panes, working through a Cairn task.
Bothy's job here is the one it is good at: be awake when nobody is, and speak up
when someone has to be. Nothing in this file asks a model anything. Drove's CLI
is run as a plain process, its JSON is read, and what it said is recorded.

THREE JOBS, kept apart because they fail differently:

  starting   `drove plan ... --detached --json` or `drove up ... --detached --json`
             returns at once with a run id. A refusal (herdr is not running, the
             task is closed) is a RECORDED REFUSAL, not a crash: Drove says why in
             a stable code and Bothy writes the code down.
  watching   `drove roster --run ID --json` until the run is down or a deadline
             passes. Persisted, so a restart picks the watch up where it was.
  listening  `drove attention --all --json`, every item that needs a person, sent
             to chat once, and a follow-up when it no longer does.

AN ACTION IS NEVER BUILT FROM A PAYLOAD. A webhook signature proves who sent the
body, not that its contents are safe to put on a command line. An action is
fixed in config; at most it names a payload field for ONE value (a Cairn ref, a
project key, a run id), and that value must match a strict pattern that cannot
start with a dash. It is passed as one argv element, never through a shell.

"I CANNOT TELL" IS NOT "DOWN". If the roster cannot be read, the run is neither
finished nor dead: it is unknown, and Bothy keeps watching and says so after a
few misses. The same goes for attention: a poll that failed never resolves
anything, because absence in a failed read proves nothing.

TOLD ONCE MEANS STORED FIRST. An item is written to disk as "to tell" before it
is sent and as "told" after, so a crash between the two sends it again rather
than never. A duplicate is the cheaper mistake when the message is "a person is
needed". The window is one send wide and is named in docs/OPERATING.md.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from . import clock
from .poll import dig
from .proc import spawn_group

__all__ = [
    "Action", "ActionError", "AttentionForwarder", "AttentionResult", "DroveCli",
    "DroveRuns", "Outcome", "StartResult", "check_action", "parse_action", "resolve_action",
]

# The first character is a letter or digit, so a value can never be read by
# Drove's argument parser as an option.
CAIRN_REF = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,31}-[0-9]{1,9}$")
PROJECT_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,31}$")
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Drove codes that can clear on their own, so the wake waits and is retried
# (counted, and abandoned out loud like any other). Everything else it refuses
# with is a statement about the request, and trying again says the same.
TRANSIENT_CODES = frozenset({"herdr-down", "herdr", "capacity"})

UNKNOWN_ALERT_AFTER = 5         # consecutive unreadable rosters before a human is told
RESOLVE_AFTER_ABSENT = 2        # successful polls an item must be missing for
MAX_SENDS_PER_POLL = 10         # a backlog is sent over several polls, not at once
MAX_DELIVERY_ATTEMPTS = 20      # then abandoned, out loud
KEEP_FINISHED = 50
TEXT_LIMIT = 700
STDERR_LIMIT = 300


class ActionError(ValueError):
    """An action that cannot be honoured. Raised when it is written, not when it fires."""


# --------------------------------------------------------------------------
# what to run


@dataclasses.dataclass(frozen=True)
class Action:
    """One thing to ask Drove to do, with every value already validated."""

    verb: str                   # plan | up
    flag: str                   # --from-cairn | --next | --run
    value: str
    repo: str | None = None

    @property
    def target(self) -> str:
        """What this run is for. Two live runs for one target is a mistake."""
        return f"{self.verb}:{self.flag.lstrip('-')}:{self.value}"

    def argv(self, binary: str) -> list[str]:
        return [binary, self.verb, self.flag, self.value, "--detached", "--json"]

    def as_dict(self) -> dict[str, Any]:
        key = {"--from-cairn": "from_cairn", "--next": "next", "--run": "run"}[self.flag]
        out: dict[str, Any] = {"action": self.verb, key: self.value}
        if self.repo:
            out["repo"] = self.repo
        return out


_SHAPES: dict[str, tuple[str, str, re.Pattern[str]]] = {
    "from_cairn": ("plan", "--from-cairn", CAIRN_REF),
    "next": ("plan", "--next", PROJECT_KEY),
    "run": ("up", "--run", RUN_ID),
}
_KNOWN = {"action", "repo", *_SHAPES}


def _build(raw: Any, payload: dict[str, Any] | None, *, template: bool) -> Action | None:
    if not isinstance(raw, dict):
        raise ActionError("a drove action is an object such as {\"action\": \"plan\", \"next\": \"DROVE\"}")
    unknown = sorted(set(raw) - _KNOWN)
    if unknown:
        raise ActionError(f"unknown key(s) in a drove action: {', '.join(unknown)}")
    named = [key for key in _SHAPES if key in raw]
    if len(named) != 1:
        raise ActionError("a drove action names exactly one of from_cairn, next or run")
    key = named[0]
    verb, flag, pattern = _SHAPES[key]
    if raw.get("action") != verb:
        raise ActionError(f"{key} belongs to action {verb!r}, not {raw.get('action')!r}")

    value = raw[key]
    if isinstance(value, dict):
        path = value.get("payload")
        if set(value) != {"payload"} or not isinstance(path, str) or not path.strip():
            raise ActionError(f"{key}: a value read from the payload is {{\"payload\": \"dotted.path\"}}")
        if template:
            value = None
        else:
            value = dig(payload or {}, path.strip())
            if not isinstance(value, str):
                raise ActionError(f"the payload has no string at {path.strip()!r} for {key}")
    if value is not None:
        # fullmatch, not match: `$` also matches before a trailing newline,
        # which a test caught letting "DROVE-5\n" through.
        if not isinstance(value, str) or not pattern.fullmatch(value):
            # The value is not echoed: it is the thing being refused, and it may
            # have come from a stranger.
            raise ActionError(f"{key} is not a valid {key.replace('_', ' ')}")

    repo = raw.get("repo")
    if repo is not None and (not isinstance(repo, str) or not os.path.isabs(repo) or "\x00" in repo):
        raise ActionError("repo is an absolute path")
    if value is None:
        return None
    return Action(verb=verb, flag=flag, value=value, repo=repo)


def check_action(raw: Any) -> None:
    """Validate an action as configured. A placeholder is accepted, a bad literal is not."""
    _build(raw, None, template=True)


def resolve_action(raw: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Fill a route's placeholder from a payload and return a plain, validated action."""
    action = _build(raw, payload, template=False)
    assert action is not None
    return action.as_dict()


def parse_action(raw: Any) -> Action:
    """A literal action, as stored on a wake. Raises ActionError if it is not one."""
    action = _build(raw, None, template=False)
    assert action is not None
    return action


# --------------------------------------------------------------------------
# running the CLI


@dataclasses.dataclass
class Outcome:
    """What one run of Drove's CLI amounted to."""

    ok: bool
    code: str = ""
    data: Any = None
    error: str = ""


def _json_object(text: str) -> dict[str, Any] | None:
    """The JSON object in a stream, whole or on its last line. Never raises."""
    candidates = [text.strip(), *reversed([line for line in text.splitlines() if line.strip()])]
    for candidate in candidates:
        with contextlib.suppress(ValueError):
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
    return None


class DroveCli:
    """Runs `drove`, with a deadline, in its own process group, and reads what it said."""

    def __init__(self, *, binary: str = "drove", home: str | None = None,
                 repo: str | None = None, timeout: float = 120.0) -> None:
        self.binary = binary
        self.home = home
        self.repo = repo
        self.timeout = timeout

    def run(self, argv: list[str], *, cwd: str | None = None, timeout: float | None = None) -> Outcome:
        env = dict(os.environ)
        if self.home:
            env["DROVE_HOME"] = self.home
        try:
            child = spawn_group(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            return Outcome(False, "not-found", error=f"{argv[0]} was not found; set drove_binary or fix PATH")
        except OSError as exc:
            return Outcome(False, "spawn-failed", error=str(exc))
        try:
            out, err = child.communicate(timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired:
            # The whole group, for the reason every worker is killed as one.
            with contextlib.suppress(OSError):
                os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
            return Outcome(False, "timeout", error=f"no answer within {timeout or self.timeout:.0f}s")

        stdout = out.decode("utf-8", "replace")
        stderr = err.decode("utf-8", "replace")
        if child.returncode == 0:
            parsed = _json_object(stdout)
            if parsed is None:
                # Exit 0 is not proof of work: a command that succeeded and
                # said nothing we can read has told us nothing.
                return Outcome(False, "unparsed", error=f"exit 0 but stdout was not JSON: {stdout.strip()[:STDERR_LIMIT]}")
            return Outcome(True, data=parsed)
        failure = _json_object(stderr) or {}
        code = failure.get("code") if isinstance(failure.get("code"), str) else f"exit-{child.returncode}"
        message = failure.get("error") if isinstance(failure.get("error"), str) else stderr.strip()
        return Outcome(False, code, error=message[:STDERR_LIMIT])


# --------------------------------------------------------------------------
# persistence


class _JsonFile:
    """One small JSON document, replaced atomically and fsynced."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, data: dict[str, Any]) -> None:
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


# --------------------------------------------------------------------------
# starting and watching runs


@dataclasses.dataclass
class StartResult:
    status: str                 # started | refused | failed
    code: str = ""
    run: str | None = None
    detail: str = ""

    @property
    def transient(self) -> bool:
        return self.status == "refused" and self.code in TRANSIENT_CODES

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def reaction_for(result: StartResult) -> str:
    """The first thing to do about a start that did not happen, carried with the alert."""
    if result.status == "failed":
        return ("A start that timed out may still have made a run: drove roster, and herdr, "
                "before starting it again.")
    return {
        "herdr-down": "Start herdr on this machine; Drove opens a workspace in it and never starts it.",
        "no-task": "That Cairn task or project has nothing to take; cairn show it.",
        "task-closed": "That Cairn task is closed; Drove will not plan a closed task.",
        "no-repo": "Set drove_repo in config.json, or give the action a repo.",
        "not-a-repo": "drove_repo (or the action's repo) is not a git repository.",
        "not-down": "That run still has a live pane. drove roster --run ID.",
    }.get(result.code, "drove with the same arguments by hand shows the whole message.")


def _after(moment: str | None, seconds: float) -> bool:
    """Whether `seconds` have passed since an ISO timestamp. Unreadable means yes."""
    if not moment:
        return True
    try:
        return clock.age_seconds(moment) >= seconds
    except (ValueError, TypeError):
        return True


class DroveRuns:
    """Starts Drove runs and watches the ones it started."""

    def __init__(self, path: str | os.PathLike[str], cli: DroveCli, audit: Any, alerter: Any, *,
                 max_runs: int = 1, deadline_seconds: float = 21600.0, poll_seconds: float = 60.0,
                 repo: str | None = None, site: str = "bothy") -> None:
        self.file = _JsonFile(path)
        self.cli = cli
        self.audit = audit
        self.alerter = alerter
        self.max_runs = max_runs
        self.deadline_seconds = deadline_seconds
        self.poll_seconds = poll_seconds
        self.repo = repo
        self.site = site

    # ---- reading ---------------------------------------------------------

    def records(self) -> list[dict[str, Any]]:
        runs = self.file.load().get("runs") or {}
        return sorted(runs.values(), key=lambda rec: str(rec.get("started_at", "")))

    def watching(self) -> list[dict[str, Any]]:
        return [rec for rec in self.records() if rec.get("status") == "watching"]

    # ---- starting --------------------------------------------------------

    def start(self, raw: Any, *, wake: str | None = None, source: str = "manual") -> StartResult:
        """Ask Drove for a run. Every outcome is recorded; none raises."""
        try:
            action = parse_action(raw)
        except ActionError as exc:
            return self._record(StartResult("refused", "action", detail=str(exc)), raw, wake, source)

        live = self.watching()
        if any(rec.get("target") == action.target for rec in live):
            return self._record(StartResult(
                "refused", "already_running",
                detail=f"a run for {action.target} is already being watched"), action, wake, source)
        if len(live) >= self.max_runs:
            return self._record(StartResult(
                "refused", "capacity",
                detail=f"{len(live)} run(s) already watched, drove_max_runs is {self.max_runs}"), action, wake, source)
        repo = action.repo or self.repo
        if not repo:
            return self._record(StartResult(
                "refused", "no-repo",
                detail="no repository: Drove binds a run to the folder it is started in. "
                       "Set drove_repo, or give the action a repo"), action, wake, source)

        outcome = self.cli.run(action.argv(self.cli.binary), cwd=repo)
        if not outcome.ok:
            status = "failed" if outcome.code in {"timeout", "unparsed", "not-found", "spawn-failed"} else "refused"
            return self._record(StartResult(status, outcome.code, detail=outcome.error), action, wake, source)
        run = outcome.data.get("run")
        if not isinstance(run, str) or not RUN_ID.fullmatch(run):
            return self._record(StartResult("failed", "unparsed", detail="Drove answered without a run id"),
                                action, wake, source)

        now = clock.utcnow()
        planner = outcome.data.get("planner") if isinstance(outcome.data.get("planner"), dict) else {}
        with self.file.lock:
            data = self.file.load()
            runs = data.setdefault("runs", {})
            runs[run] = {
                "run": run, "target": action.target, "action": action.as_dict(), "wake": wake,
                "source": source, "status": "watching", "started_at": clock.iso(now),
                "deadline_at": clock.iso(now + _dt.timedelta(seconds=self.deadline_seconds)),
                "planner_state": planner.get("state"), "workspace": outcome.data.get("workspace"),
                "goal": outcome.data.get("goal"), "unknown": 0, "checked_at": None,
            }
            self.file.save(data)
        return self._record(StartResult("started", run=run, detail=str(outcome.data.get("goal") or "")),
                            action, wake, source)

    def _record(self, result: StartResult, action: Any, wake: str | None, source: str) -> StartResult:
        target = action.target if isinstance(action, Action) else None
        self.audit.append(
            kind="drove", action=result.status,
            status={"started": "ok", "refused": "refused"}.get(result.status, "failed"),
            subject=target,
            data={"wake": wake, "source": source, "code": result.code, "drove_run": result.run,
                  "detail": result.detail[:STDERR_LIMIT]},
        )
        return result

    # ---- watching --------------------------------------------------------

    def tick(self) -> int:
        """Read each watched run's roster once if it is due. Returns how many it read."""
        read = 0
        for rec in self.watching():
            deadline_passed = _after(rec.get("deadline_at"), 0.0)
            if not deadline_passed and not _after(rec.get("checked_at"), self.poll_seconds):
                continue
            read += 1
            outcome = self.cli.run([self.cli.binary, "roster", "--run", rec["run"], "--json"], timeout=60.0)
            self._observe(rec, outcome, deadline_passed=deadline_passed)
        return read

    def _observe(self, rec: dict[str, Any], outcome: Outcome, *, deadline_passed: bool) -> None:
        run = rec["run"]
        if not outcome.ok and outcome.code == "no-run":
            self._finish(rec, "vanished", detail="Drove has no run by that name any more")
            return
        status = planner = last = None
        if outcome.ok and isinstance(outcome.data, dict):
            body = outcome.data
            status = (body.get("run") or {}).get("status") if isinstance(body.get("run"), dict) else None
            planner = (body.get("planner") or {}).get("state") if isinstance(body.get("planner"), dict) else None
            last = body.get("lastReport") if isinstance(body.get("lastReport"), dict) else None

        if status == "down":
            self._finish(rec, "down", last=last)
            return

        changes: dict[str, Any] = {"checked_at": clock.iso()}
        if status == "up":
            changes["unknown"] = 0
            if planner != rec.get("planner_state"):
                self.audit.append(kind="drove", action="state", subject=rec.get("target"),
                                  data={"drove_run": run, "planner_state": planner,
                                        "was": rec.get("planner_state")})
                changes["planner_state"] = planner
        else:
            # Unreadable, or a status we do not know: neither finished nor dead.
            unknown = int(rec.get("unknown", 0)) + 1
            changes["unknown"] = unknown
            if unknown == UNKNOWN_ALERT_AFTER:
                self.alerter.incident(
                    job=f"drove:{run}", severity="warning",
                    error=f"cannot read the state of run {run}: {outcome.code or 'no status'} {outcome.error}".strip(),
                    reaction=f"drove roster --run {run} --json; the run is being kept as watched, not assumed down",
                )
        self._update(run, changes)
        if deadline_passed:
            self._finish({**rec, **changes}, "deadline")

    def _update(self, run: str, changes: dict[str, Any]) -> None:
        with self.file.lock:
            data = self.file.load()
            entry = (data.get("runs") or {}).get(run)
            if entry is not None:
                entry.update(changes)
                self.file.save(data)

    def _finish(self, rec: dict[str, Any], how: str, *, last: dict[str, Any] | None = None,
                detail: str = "") -> None:
        run = rec["run"]
        self._update(run, {"status": how, "finished_at": clock.iso(), "checked_at": clock.iso(),
                           "last_report": last})
        self._prune()
        self.audit.append(kind="drove", action="finished", status="ok" if how == "down" else "failed",
                          subject=rec.get("target"),
                          data={"drove_run": run, "how": how, "started_at": rec.get("started_at"),
                                "detail": detail, "last_report": last})
        if how == "down":
            said = f"{last.get('seat', '?')} {last.get('kind', 'report')}: {last.get('summary', '')}" if last \
                else "no report was recorded"
            self.alerter.say(f"**{self.site}** — Drove run `{run}` is down ({rec.get('target')}). "
                             f"Last: {_clip(said, 400)}")
        elif how == "deadline":
            self.alerter.incident(
                job=f"drove:{run}", severity="warning",
                error=f"run {run} was still up after {self.deadline_seconds / 3600:.1f}h; "
                      f"Bothy stopped watching it and did not stop it",
                reaction=f"drove roster --run {run}; drove down --run {run} to stop it",
            )
        else:
            self.alerter.incident(job=f"drove:{run}", severity="warning",
                                  error=f"run {run} is gone from Drove: {detail}",
                                  reaction="drove roster --run to confirm; its record here is closed")

    def _prune(self) -> None:
        with self.file.lock:
            data = self.file.load()
            runs = data.get("runs") or {}
            done = sorted((r for r in runs.values() if r.get("status") != "watching"),
                          key=lambda r: str(r.get("finished_at", "")))
            for stale in done[:-KEEP_FINISHED] if KEEP_FINISHED else done:
                runs.pop(stale["run"], None)
            data["runs"] = runs
            self.file.save(data)


def _clip(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# --------------------------------------------------------------------------
# what needs a person


@dataclasses.dataclass
class AttentionResult:
    """What one poll did, for the audit log and for an operator."""

    status: str                 # ok | failed
    seen: int = 0               # items needing a person right now
    told: int = 0
    resolved: int = 0
    pending: int = 0            # still to send, because of the per-poll cap or a failed delivery
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _neutralise(text: str) -> str:
    """Make worker-written text safe to post to a channel.

    The text of an item was written by a model reading untrusted material. In a
    chat it must not be able to ping a channel or open a code fence of its own.
    """
    return (text.replace("`", "'").replace("@", "@​").replace("<!", "<​!"))


def format_item(site: str, item: dict[str, Any]) -> str:
    answer = item.get("answer") if isinstance(item.get("answer"), dict) else {}
    if answer.get("type") == "command":
        how = f"`{_neutralise(str(answer.get('command', '')))}`"
    else:
        pane = f" `{_neutralise(str(answer['pane']))}`" if answer.get("pane") else ""
        inspect = f" · `{_neutralise(str(answer['inspect']))}`" if answer.get("inspect") else ""
        how = f"answer it in the pane{pane}{inspect}"
    return "\n".join([
        f"!! **NEEDS A PERSON** `{site}` — Drove {_neutralise(str(item.get('kind', '?')))}",
        f"run `{_neutralise(str(item.get('run', '?')))}` · seat `{_neutralise(str(item.get('seat', '?')))}`"
        f" · since {_neutralise(str(item.get('since', '?')))}",
        f"> {_neutralise(_clip(str(item.get('text', '')), TEXT_LIMIT))}",
        f"→ {how}",
        f"id `{_neutralise(str(item.get('id', '?')))}`",
    ])


def format_resolved(site: str, item: dict[str, Any], *, run_up: bool) -> str:
    why = "answered or cleared" if run_up else "the run is no longer up"
    return (f"✓ `{site}` — no longer waiting on a person ({why}): "
            f"{_neutralise(str(item.get('kind', '?')))} · run `{_neutralise(str(item.get('run', '?')))}` · "
            f"seat `{_neutralise(str(item.get('seat', '?')))}` · id `{_neutralise(str(item.get('id', '?')))}`")


class AttentionForwarder:
    """Sends each item that needs a person to chat once, and says when it stops.

    One state file, keyed by Drove's item id, which Drove keeps stable for as
    long as an item is open and never reuses. Each entry is in one of:

        new        seen, not yet sent
        open       sent; waiting for it to stop being in the snapshot
        closing    gone for RESOLVE_AFTER_ABSENT polls; the follow-up is not yet sent
        abandoned  could not be delivered MAX_DELIVERY_ATTEMPTS times; kept so it
                   is not announced again, dropped once it is gone

    ITEMS THE PLANNER HANDLES ARE NOT FORWARDED. Drove addresses some items to
    its planner (`for: planner`, or on older Drove `answer.type: planner`) --
    stalled, idle-without-report, budget, pending-delivery -- as nothing for a
    person unless the planner asks. Sending them would teach whoever reads the
    channel to stop reading it.
    """

    def __init__(self, path: str | os.PathLike[str], cli: DroveCli, deliver: Callable[[str], bool],
                 audit: Any, *, site: str = "bothy", alerter: Any = None) -> None:
        self.file = _JsonFile(path)
        self.cli = cli
        self.deliver = deliver
        self.audit = audit
        self.site = site
        self.alerter = alerter

    def snapshot(self) -> tuple[list[dict[str, Any]], set[str], str]:
        """Read Drove. Returns (items for a person, runs that are up, error)."""
        outcome = self.cli.run([self.cli.binary, "attention", "--all", "--json"], timeout=60.0)
        if not outcome.ok:
            return [], set(), f"{outcome.code}: {outcome.error}".strip()
        body = outcome.data
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(body, dict) or body.get("v") != 1 or not isinstance(items, list):
            return [], set(), "unexpected shape from drove attention (want v 1 with an items list)"
        wanted: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            if not _for_a_person(item):
                continue
            wanted.append(item)
        runs = {str(run) for run in body.get("runs") or [] if isinstance(run, str)}
        return wanted, runs, ""

    def poll(self) -> AttentionResult:
        items, runs, error = self.snapshot()
        if error:
            # A failed read never resolves anything and never marks anything.
            return AttentionResult("failed", detail=error)
        result = AttentionResult("ok", seen=len(items))
        current = {item["id"]: item for item in items}

        with self.file.lock:
            data = self.file.load()
            entries: dict[str, dict[str, Any]] = data.get("items") or {}
            for item_id, item in current.items():
                entry = entries.get(item_id)
                if entry is None:
                    entries[item_id] = {"state": "new", "attempts": 0, "absent": 0,
                                        "first_seen_at": clock.iso(), "item": item}
                else:
                    entry["absent"] = 0
                    entry["item"] = item
                    if entry["state"] == "closing":
                        entry["state"], entry["attempts"] = "open", 0
            for item_id in [key for key in entries if key not in current]:
                entry = entries[item_id]
                entry["absent"] = int(entry.get("absent", 0)) + 1
                if entry["absent"] < RESOLVE_AFTER_ABSENT:
                    continue
                if entry["state"] == "open":
                    entry["state"], entry["attempts"] = "closing", 0
                elif entry["state"] in {"new", "abandoned"}:
                    # Never delivered, or given up on: there is nothing to follow up.
                    self.audit.append(kind="drove", action="attention-dropped", subject=item_id,
                                      data={"was": entry["state"], "item": _brief(entry["item"])})
                    del entries[item_id]
            data["items"] = entries
            self.file.save(data)

            sent = 0
            for item_id in sorted(entries, key=lambda key: (entries[key].get("first_seen_at", ""), key)):
                if sent >= MAX_SENDS_PER_POLL:
                    break
                entry = entries[item_id]
                if entry["state"] == "new":
                    message = format_item(self.site, entry["item"])
                elif entry["state"] == "closing":
                    message = format_resolved(self.site, entry["item"],
                                              run_up=str(entry["item"].get("run")) in runs)
                else:
                    continue
                sent += 1
                delivered = self._deliver(message)
                entry["attempts"] = int(entry.get("attempts", 0)) + 1
                if delivered and entry["state"] == "new":
                    entry["state"], entry["attempts"] = "open", 0
                    entry["notified_at"] = clock.iso()
                    result.told += 1
                    self.audit.append(kind="drove", action="attention-forwarded", subject=item_id,
                                      data=_brief(entry["item"]))
                elif delivered:
                    del entries[item_id]
                    result.resolved += 1
                    self.audit.append(kind="drove", action="attention-resolved", subject=item_id,
                                      data=_brief(entry["item"]))
                elif entry["attempts"] >= MAX_DELIVERY_ATTEMPTS:
                    self._give_up(item_id, entries)
                self.file.save(data)
            result.pending = sum(1 for entry in entries.values() if entry["state"] in {"new", "closing"})
        return result

    def _deliver(self, message: str) -> bool:
        try:
            return bool(self.deliver(message))
        except Exception:  # noqa: BLE001 - delivery must not take the poll down with it
            return False

    def _give_up(self, item_id: str, entries: dict[str, dict[str, Any]]) -> None:
        entry = entries[item_id]
        was = entry["state"]
        self.audit.append(kind="drove", action="attention-abandoned", status="failed", subject=item_id,
                          data={"was": was, "attempts": entry["attempts"], "item": _brief(entry["item"])})
        if self.alerter is not None:
            self.alerter.incident(
                job="drove:attention", severity="critical",
                error=f"could not deliver {item_id} after {entry['attempts']} attempts",
                reaction="Check the webhook URL. The item is still open in Drove: drove attention --all",
            )
        if was == "new":
            entry["state"] = "abandoned"
        else:
            del entries[item_id]

    def view(self) -> tuple[list[tuple[dict[str, Any], str]], str]:
        """What Drove says needs a person now, each with what Bothy has done about it.

        Reads and writes nothing: this is the by-hand answer to "why did that
        not reach chat". `unseen` means the next poll will send it.
        """
        items, _, error = self.snapshot()
        known = self.file.load().get("items") or {}
        return [(item, str(known.get(item["id"], {}).get("state", "unseen"))) for item in items], error


def _for_a_person(item: dict[str, Any]) -> bool:
    """Whether Drove addresses an item to a person rather than to its planner.

    Newer Drove says so in `for` ("human" or "planner"), and when it does that
    is the answer. Older Drove has no such field and marks the planner's items
    with `answer.type: "planner"`, so that is the fallback; with neither, an
    item is a person's. The `--for` flag is deliberately not passed: an older
    Drove would refuse it, and the default is human anyway.
    """
    audience = item.get("for")
    if isinstance(audience, str):
        return audience == "human"
    answer = item.get("answer") if isinstance(item.get("answer"), dict) else {}
    return answer.get("type") != "planner"


def _brief(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in ("id", "run", "seat", "kind", "since")}
