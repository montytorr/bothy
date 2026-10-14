"""Driving Codex as a supervised worker, not a shell command.

Bothy talks to ``codex app-server --listen stdio://``: newline-delimited
JSON-RPC over the worker's stdin and stdout. The alternative, ``codex exec
--json``, gives eight coarse event types and no way to intervene — you learn
what happened by reading the transcript afterwards. The app-server gives tool
calls BEFORE they execute, streamed output, approval requests addressed to us,
and ``turn/interrupt``. For something running unattended on a client's machine,
being able to say "no" and "stop" is the entire justification for the extra
protocol work.

Four things this module is careful about, each for a recorded reason:

  PROCESS GROUPS.    The worker is a tree — a Node shim, a Rust binary, and
                     sometimes a code-mode host. Spawned and killed as a group,
                     via proc.py.

  ITS OWN HOME.      Each worker gets its own CODEX_HOME so concurrent workers
                     do not share SQLite. Five app-servers against one home did
                     test clean, but busy_timeout reads back as 0 on a fresh
                     connection and the shared WAL is already megabytes; paying
                     a directory per run is much cheaper than debugging that.

  A MONOTONIC CLOCK. Every deadline is measured with monotonic time, so a clock
                     correction mid-run cannot cut a turn short or hang it.

  NOTHING SILENT.    Unknown notifications are surfaced, not dropped. The
                     protocol is explicitly experimental and moved twice in a
                     fortnight; a client that quietly ignores what it does not
                     recognise will keep working and stop being true.
"""

from __future__ import annotations

import dataclasses
import json
import os
import queue
import secrets
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from . import clock, policy, proc
from .capability import ToolRegistry
from .interrupts import control_transaction, pending_owner, write_control

__all__ = [
    "CodexError",
    "CodexTimeout",
    "ApprovalDecision",
    "TurnOutcome",
    "CodexClient",
    "default_approval",
    "default_question",
    "Question",
    "PendingInterrupt",
    "StopReason",
]

# Notification HTTP can stall in DNS despite a socket timeout. Bound observers
# across runs so a failed chat endpoint cannot accumulate immortal threads.
_NOTIFY_SLOTS = threading.BoundedSemaphore(2)
_MAX_PENDING_INTERRUPTS = 16

StopReason = Literal["wall_clock", "token_cap", "budget", "awaiting_human", "cancelled"]


class CodexError(RuntimeError):
    """The worker failed in a way the caller must know about."""


class CodexTimeout(CodexError):
    """A request or a turn outran its deadline."""


# --------------------------------------------------------------------------
# approvals


@dataclasses.dataclass(frozen=True)
class ApprovalDecision:
    """What Bothy answers when Codex asks permission.

    ``decline`` lets the turn continue having been refused, which is usually
    what you want: the model can explain itself or try another way. ``cancel``
    interrupts the turn outright and is for things that should end the run.
    """

    decision: str  # accept | acceptForSession | decline | cancel
    reason: str = ""
    hold: bool = False
    server: str = ""
    tool: str = ""


def default_approval(method: str, params: dict[str, Any]) -> ApprovalDecision:
    """Deny by default, and say so.

    A harness running unattended on someone else's machine has no business
    approving anything it was not explicitly configured to approve. The safe
    default is to decline and let the turn continue, so the transcript records
    a refusal rather than a silent success.
    """
    return ApprovalDecision("decline", reason=f"no approval policy configured for {method}")


# Checked against `codex app-server generate-json-schema` 0.162.0.
# MCP _meta approval keys are observed binary metadata; the schema leaves _meta
# untyped. policy.identify() keeps that distinction and declines real forms.
def question_answer(params: dict[str, Any], text: str) -> dict[str, Any]:
    questions = params.get("questions")
    questions = questions if isinstance(questions, list) else []
    return {"answers": {q["id"]: {"answers": [text]} for q in questions
                        if isinstance(q, dict) and isinstance(q.get("id"), str)}}


def approval_answer(method: str, decision: str, reason: str = "") -> dict[str, Any]:
    if method == "mcpServer/elicitation/request":
        action = decision if decision in {"accept", "cancel"} else "decline"
        return {"action": action, **({"content": {}} if action == "accept" else {})}
    if method == "item/permissions/requestApproval":
        # GrantedPermissionProfile has no required fields: {} grants nothing.
        return {"permissions": {}, "scope": "turn"}
    if method in {"applyPatchApproval", "execCommandApproval"}:
        legacy = {"accept": "approved", "acceptForSession": "approved_for_session", "cancel": "abort"}
        return {"decision": legacy.get(decision, {"denied": {"rejection": reason or "declined by Bothy"}})}
    # Modern command/file decisions have no reason field. The audit holds it.
    return {"decision": decision}


UNATTENDED_GUIDANCE = (
    "Bothy runs unattended and cannot wait for an answer. "
    "Your question has been recorded and sent to an operator. "
    "Continue with what you can do without it, or stop and say "
    "what you are blocked on.")


def question_decline(method: str = "item/tool/requestUserInput", params: dict[str, Any] | None = None) -> dict[str, Any]:
    guidance = UNATTENDED_GUIDANCE
    if params and secret_question(params):
        guidance = "Bothy cannot collect secrets through chat. This question was declined; continue without it."
    if method == "item/tool/requestUserInput":
        return question_answer(params or {}, guidance)
    return {"decision": "decline", "reason": guidance}


def secret_question(params: dict[str, Any]) -> bool:
    questions = params.get("questions")
    return isinstance(questions, list) and any(
        isinstance(q, dict) and q.get("isSecret") for q in questions)


@dataclasses.dataclass
class PendingInterrupt:
    id: str
    rpc_id: Any
    deadline: float
    kind: str
    method: str
    params: dict[str, Any]
    prompt: str
    started: float
    server: str = ""
    tool: str = ""
    text: str | None = None
    actor: str = ""
    announced: bool = False
    announcing: bool = False
    announcement_failed: bool = False
    asked_at: str = dataclasses.field(default_factory=clock.iso)
    question: Question | None = None


def _question_text(params: dict[str, Any]) -> str:
    """Pull the human-readable question out of a requestUserInput payload.

    The app-server has carried it under several keys across versions, and a
    question nobody can read is no better than one nobody was told about — so
    try the known shapes, then fall back to the whole payload rather than
    reporting an empty string.
    """
    questions = params.get("questions")
    if isinstance(questions, list) and questions:
        return "\n".join(_question_text(q) for q in questions if isinstance(q, dict))
    for key in ("prompt", "question", "message", "text", "title"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # Some versions nest it one level down.
    for container in ("input", "request", "userInput"):
        nested = params.get(container)
        if isinstance(nested, dict):
            inner = _question_text(nested)
            if inner:
                return inner
    return json.dumps(params, default=str)[:2000]


def default_question(question: "Question") -> None:
    """Do nothing, loudly — a caller that supplies no handler still gets the
    question on the outcome, where the audit log will keep it."""
    return None


# --------------------------------------------------------------------------
# outcome


@dataclasses.dataclass
class Question:
    """Something the agent stopped to ask a person.

    Codex asks through `.../requestUserInput`, which is NOT a permission
    request: it is the model saying it needs information only a human has.
    Bothy used to route it through the approval handler and decline it in
    milliseconds with nobody told — the one moment the agent reaches for a
    person is the one moment it was guaranteed not to get one.
    """

    method: str
    prompt: str
    params: dict[str, Any] = dataclasses.field(default_factory=dict)
    asked_at: str = ""
    interrupt_id: str | None = None
    answered: bool = False
    secret: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"method": self.method, "prompt": self.prompt, "asked_at": self.asked_at,
                **({"interrupt_id": self.interrupt_id, "answered": self.answered} if self.interrupt_id else {}),
                **({"secret": True} if self.secret else {})}


@dataclasses.dataclass
class TurnOutcome:
    """What one turn did, in the terms Bothy records and bills."""

    status: str                      # completed | failed | interrupted
    thread_id: str | None = None
    turn_id: str | None = None
    messages: list[str] = dataclasses.field(default_factory=list)
    usage: dict[str, Any] = dataclasses.field(default_factory=dict)
    rate_limits: dict[str, Any] = dataclasses.field(default_factory=dict)
    error: str | None = None
    error_code: str | None = None
    items: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    #: All questions this turn, including whether a bounded hold was answered.
    #: Expired questions survive on the outcome for the operator to investigate.
    questions: list[Question] = dataclasses.field(default_factory=list)
    stop_reason: StopReason | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# --------------------------------------------------------------------------
# the client


class CodexClient:
    """One supervised Codex app-server, for the life of one run."""

    def __init__(
        self,
        *,
        run_id: str,
        codex_home: str | os.PathLike[str],
        binary: str = "codex",
        client_name: str = "bothy",
        version: str = "0.1.0",
        env: dict[str, str] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        approval: Callable[[str, dict[str, Any]], ApprovalDecision] = default_approval,
        on_question: Callable[[Question], None] = default_question,
        on_tool_call: Callable[[dict[str, Any]], None] | None = None,
        tool_approval: Callable[[dict[str, Any]], ApprovalDecision] | None = None,
        tools: "ToolRegistry | None" = None,
        question_hold_seconds: float = 0,
        reply_channel: bool = False,
        control_dir: Path | None = None,
        on_interrupt: Callable[[PendingInterrupt], bool] | None = None,
        on_answer: Callable[[PendingInterrupt, str, str], bool] | None = None,
        on_resolved: Callable[[PendingInterrupt, str], None] | None = None,
    ) -> None:
        self.run_id = run_id
        self.codex_home = Path(codex_home)
        self.binary = binary
        self.client_name = client_name
        self.version = version
        self._env = env
        self._on_event = on_event or (lambda method, params: None)
        self._approval = approval
        self._on_question = on_question
        self._on_tool_call = on_tool_call
        self._tool_approval = tool_approval
        #: Every question asked this run, in order. Read by run_turn onto the
        #: outcome so they reach the audit log and the operator.
        self.questions: list[Question] = []
        self._tools = tools
        self.question_hold_seconds = question_hold_seconds
        self.reply_channel = reply_channel
        self._control_owner: dict[str, Any] | None = None
        self.control_dir = control_dir
        self._on_interrupt = on_interrupt
        self._on_answer = on_answer
        self._on_resolved = on_resolved
        self.interrupts: dict[str, PendingInterrupt] = {}
        self._holds_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._hold_used = 0.0
        self._hold_limit: float | None = None

        self._popen: subprocess.Popen | None = None
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._stderr_tail: list[str] = []
        self._closed = threading.Event()
        self._notifications: queue.Queue = queue.Queue()
        self._stop_reason: StopReason | None = None
        self._interrupt_lock = threading.Lock()

    # ---- lifecycle -----------------------------------------------------

    @property
    def pgid(self) -> int | None:
        return self._popen.pid if self._popen is not None else None

    @property
    def popen(self) -> subprocess.Popen | None:
        return self._popen

    def start(self) -> subprocess.Popen:
        """Spawn the app-server in its own process group and begin reading."""
        self.codex_home.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ if self._env is None else self._env)
        env["CODEX_HOME"] = str(self.codex_home)
        # The parent's Node tuning is not the child's: an inherited NODE_OPTIONS
        # once broke Codex's own setup probe on the reference host.
        env.pop("NODE_OPTIONS", None)

        argv = [self.binary, "app-server", "--listen", "stdio://"]
        self._popen = proc.spawn_group(argv, env=env)
        self._reader = threading.Thread(target=self._read_loop, name=f"codex-read-{self.run_id}", daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._read_stderr, name=f"codex-err-{self.run_id}", daemon=True
        )
        self._stderr_reader.start()
        return self._popen

    def close(self, *, grace_seconds: float = 10.0) -> str:
        """Kill the whole worker group and reap it. Always safe to call."""
        self._closed.set()
        try:
            self._tick_interrupts(stopping=True)
        except CodexError:
            pass  # the worker is already gone; its process group is still reaped
        if self._popen is None:
            return "never-started"
        pgid = self._popen.pid
        outcome = proc.kill_group(pgid, grace_seconds=grace_seconds)
        try:
            self._popen.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            pass
        for stream in (self._popen.stdin, self._popen.stdout, self._popen.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        return outcome

    def __enter__(self) -> "CodexClient":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- wire ----------------------------------------------------------

    def _read_stderr(self) -> None:
        """Keep the tail of stderr, so a startup failure has a cause attached."""
        stream = self._popen.stderr if self._popen else None
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self._stderr_tail.append(line)
                del self._stderr_tail[:-50]

    def _read_loop(self) -> None:
        """Demultiplex responses, notifications and server-to-client requests."""
        stream = self._popen.stdout if self._popen else None
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue

            if "id" in message and ("result" in message or "error" in message):
                self._deliver_response(message)
            elif "method" in message and "id" in message:
                self._handle_server_request(message)
            elif "method" in message:
                params = message.get("params") or {}
                if message["method"] in {"item/started", "item/completed"}:
                    params = {**params, "observed_at": clock.iso()}
                self._notifications.put((message["method"], params))
                try:
                    self._on_event(message["method"], params)
                except Exception:  # noqa: BLE001 - an observer must never kill the reader
                    pass
        self._closed.set()
        # Wake anything still waiting, rather than letting it sit out its timeout.
        with self._pending_lock:
            for waiter in self._pending.values():
                waiter.put({"error": {"message": "app-server closed the connection"}})

    def _deliver_response(self, message: dict[str, Any]) -> None:
        with self._pending_lock:
            waiter = self._pending.pop(message["id"], None)
        if waiter is not None:
            waiter.put(message)

    def _handle_server_request(self, message: dict[str, Any]) -> None:
        """Answer a request Codex addressed to us — approvals, mostly."""
        method = str(message.get("method"))
        params = message.get("params") or {}
        params = params if isinstance(params, dict) else {}
        hidden = method.endswith("/requestUserInput") and secret_question(params)
        if hidden:
            # Do not expose even the secret question's header/options/prompt to
            # observers, the outcome or chat. Decline the entire batch.
            params = {"secret": True}
        try:
            self._on_event(method, params)
        except Exception:  # noqa: BLE001
            pass
        if method == "item/tool/call":
            # A tool Bothy declared on thread/start. The handler runs inside this
            # process, which is the point: it can touch the ledger, the checklist
            # and the audit log directly, with no subprocess, port or credential.
            self._handle_tool_call(message, params)
        elif method.endswith("/requestUserInput"):
            question = Question(method=method, prompt="[secret question declined]" if hidden else _question_text(params),
                                params=params, asked_at=clock.iso(), secret=hidden)
            self.questions.append(question)
            if not hidden and self._hold(message, "question", question.prompt, question=question):
                return
            try:
                self._on_question(question)
            except Exception:  # noqa: BLE001
                pass
            self._send({"id": message["id"], "result": question_decline(method, message.get("params") or {})})
        elif ("requestApproval" in method or method in {
                "mcpServer/elicitation/request", "applyPatchApproval", "execCommandApproval"}):
            verdict = self._decide(method, params)
            if method == "item/permissions/requestApproval" or (
                    method == "mcpServer/elicitation/request" and not policy.mcp_tool_approval(params)):
                verdict = ApprovalDecision("decline", reason="permission grants and non-tool elicitations are unsupported")
            if verdict.hold and self._hold(message, "approval", f"Approve {verdict.server}.{verdict.tool}: {verdict.reason}",
                                           server=verdict.server, tool=verdict.tool):
                return
            self._send({"id": message["id"],
                        "result": approval_answer(method, verdict.decision, verdict.reason)})
        else:
            # Unknown request: refuse explicitly rather than hanging the turn.
            # The policy hears of it first, so a request under a method name we
            # did not anticipate is audited instead of vanishing.
            self._decide(method, params)
            self._send(
                {
                    "id": message["id"],
                    "error": {"code": -32601, "message": f"{self.client_name} does not implement {method}"},
                }
            )

    def _hold(self, message: dict[str, Any], kind: str, prompt: str,
              *, server: str = "", tool: str = "", question: Question | None = None) -> bool:
        if (self.question_hold_seconds <= 0 or self._closed.is_set() or self._stop_reason is not None
                or not self.reply_channel or self._on_interrupt is None):
            return False
        if kind == "question" and not question_answer(message.get("params") or {}, "")["answers"]:
            # Unknown/legacy question payloads cannot be answered from ids we do
            # not have. Keep their existing recorded decline instead of guessing.
            return False
        if len(self.interrupts) >= _MAX_PENDING_INTERRUPTS:
            return False
        now = clock.monotonic()
        deadline = now + min(self.question_hold_seconds, 5.0)
        if self._hold_limit is not None:
            deadline = min(deadline, self._hold_limit)
        if deadline <= now:
            return False
        pending = PendingInterrupt("int_" + secrets.token_hex(16), message["id"], deadline,
                                   kind, str(message["method"]), message.get("params") or {},
                                   prompt, now, server, tool)
        pending.question = question
        if question is not None:
            question.interrupt_id = pending.id
        with self._holds_lock:
            self.interrupts[pending.id] = pending
        return True

    def answer(self, interrupt_id: str, text: str, actor: str = "cli") -> bool:
        """First answer wins; expired and unknown holds cannot approve anything."""
        with self._holds_lock:
            pending = self.interrupts.get(interrupt_id)
            if (pending is None or self._closed.is_set() or self._stop_reason is not None
                    or not pending.announced or pending.text is not None or clock.monotonic() >= pending.deadline):
                return False
            if self._on_answer is not None and not self._on_answer(pending, text, actor):
                return False
            pending.text, pending.actor = text, actor
            return True

    def activate_interrupt(self, pending: PendingInterrupt, register: Callable[[], None] | None = None) -> bool:
        """Publish a CLI marker only after a chat post or foreground hint succeeds."""
        deadline = clock.monotonic() + self.question_hold_seconds
        if self._hold_limit is not None:
            deadline = min(deadline, self._hold_limit)
        if self.control_dir is not None:
            if self._control_owner is None:
                self._control_owner = pending_owner(self.run_id)
            with control_transaction(self.control_dir):
                write_control(self.control_dir, f"{pending.id}.pending",
                              {"owner": self._control_owner, "deadline": deadline})
        with self._holds_lock:
            if (self.interrupts.get(pending.id) is pending and not self._closed.is_set()
                    and clock.monotonic() < pending.deadline):
                pending.deadline = deadline
                pending.started = clock.monotonic()
                pending.announced = True
                if register is not None:
                    register()  # memory-only board registration; no outbound work
                return True
        self._clear_control(pending.id)
        return False

    def _clear_control(self, interrupt_id: str) -> None:
        if self.control_dir is not None:
            with control_transaction(self.control_dir):
                (self.control_dir / f"{interrupt_id}.pending").unlink(missing_ok=True)
                (self.control_dir / f"{interrupt_id}.answer").unlink(missing_ok=True)

    def _tick_interrupts(self, *, stopping: bool = False) -> None:
        with self._holds_lock:
            snapshot = list(self.interrupts.values())
        announcements = []
        for pending in snapshot:
            with self._holds_lock:
                if (not pending.announced and not pending.announcing and not stopping
                        and not pending.announcement_failed and clock.monotonic() < pending.deadline
                        and _NOTIFY_SLOTS.acquire(blocking=False)):
                    pending.announcing = True
                    announcements.append(pending)
        # Start observers outside the lock, including deterministic test observers.
        for pending in announcements:
            try:
                threading.Thread(target=self._announce_interrupt, args=(pending,), daemon=True).start()
            except RuntimeError:
                _NOTIFY_SLOTS.release()
                with self._holds_lock:
                    pending.announcement_failed = True
        if not stopping and self.control_dir is not None:
            for pending in snapshot:
                path = self.control_dir / f"{pending.id}.answer"
                with control_transaction(self.control_dir):
                    try:
                        body = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        body = None
                    path.unlink(missing_ok=True)
                if isinstance(body, dict) and isinstance(body.get("text"), str):
                    self.answer(pending.id, body["text"], "cli")
        resolved = []
        with self._holds_lock:
            for pending in snapshot:
                if self.interrupts.get(pending.id) is not pending:
                    continue
                if pending.text is None and not stopping and not pending.announcement_failed and clock.monotonic() < pending.deadline:
                    continue
                del self.interrupts[pending.id]
                state = ("stopped" if stopping else "answered" if pending.text is not None else
                         "unavailable" if pending.announcement_failed else "expired")
                resolved.append((pending, state))
        # No hosted handler, wire write, control-file I/O or observer callback
        # holds _holds_lock. A slow approved tool cannot stall another answer.
        for pending, state in resolved:
            self._clear_control(pending.id)
            if self._on_resolved is not None:
                self._on_resolved(pending, state)
            if pending.method == "item/tool/call":
                accepted = state == "answered" and pending.text.strip().lower() in {"approve", "yes"}
                self._handle_tool_call({"id": pending.rpc_id}, pending.params, approved=True,
                                       refused=None if accepted else "human approval declined")
                continue
            if state == "answered":
                result = (question_answer(pending.params, pending.text) if pending.kind == "question"
                          else approval_answer(pending.method, "accept" if
                               pending.text.strip().lower() in {"approve", "yes"} else "decline"))
            else:
                result = (question_decline(pending.method, pending.params) if pending.kind == "question"
                          else approval_answer(pending.method, "decline"))
            self._send({"id": pending.rpc_id, "result": result})
            if state == "answered" and pending.question is not None:
                pending.question.answered = True

    def _announce_interrupt(self, pending: PendingInterrupt) -> None:
        try:
            delivered = self._on_interrupt(pending)
            if delivered:
                if not pending.announced:
                    delivered = self.activate_interrupt(pending)
            if not delivered:
                with self._holds_lock:
                    pending.announcement_failed = True
        except Exception:  # noqa: BLE001 - notification failure declines at the next tick
            with self._holds_lock:
                pending.announcement_failed = True
        finally:
            _NOTIFY_SLOTS.release()

    def _decide(self, method: str, params: dict[str, Any]) -> ApprovalDecision:
        """Ask the approval policy. A policy that fails declines; it never approves."""
        try:
            return self._approval(method, params)
        except Exception as exc:  # noqa: BLE001
            return ApprovalDecision("decline", reason=f"approval policy failed: {type(exc).__name__}: {exc}")

    def _handle_tool_call(self, message: dict[str, Any], params: dict[str, Any],
                          *, approved: bool = False, refused: str | None = None) -> None:
        """Answer a dynamic tool call. Never raises, always replies.

        A tool that fails answers ``success: false`` with the reason as text, so
        the model can read what went wrong and carry on. Leaving the request
        unanswered would hang the turn until its wall clock, which turns a small
        tool bug into a lost run.
        """
        name = str(params.get("tool") or "")
        if self._tool_approval is not None and not approved:
            try:
                verdict = self._tool_approval(params)
            except Exception as exc:  # noqa: BLE001 - a failing gate always denies
                verdict = ApprovalDecision("decline", reason=f"tool approval failed: {exc}")
            if verdict.hold and self._hold({**message, "method": "item/tool/call", "params": params},
                                           "approval", f"Approve {verdict.server}.{verdict.tool}: {verdict.reason}",
                                           server=verdict.server, tool=verdict.tool):
                return
            if verdict.decision not in {"accept", "acceptForSession"}:
                refused = verdict.reason or "tool approval declined"
        started = clock.iso()
        if refused is not None:
            ok, text = False, refused
        elif self._tools is None:
            ok, text = False, f"{name} is not available to this run"
        else:
            ok, text = self._tools.call(name, params.get("arguments"))
        if self._on_tool_call is not None:
            arguments = params.get("arguments")
            try:
                self._on_tool_call({
                    "call_id": str(message["id"]), "tool": name,
                    "turn_id": params.get("turnId"), "thread_id": params.get("threadId"),
                    "arguments": (arguments if isinstance(arguments, str) else
                                  json.dumps(arguments, ensure_ascii=False, default=str))[:500],
                    "start": started, "end": clock.iso(), "status": "ok" if ok else "failed",
                })
            except Exception:  # noqa: BLE001 - an observer must not strand a tool reply
                pass
        self._send({
            "id": message["id"],
            "result": {"success": ok, "contentItems": [{"type": "inputText", "text": text}]},
        })

    def _send(self, payload: dict[str, Any]) -> None:
        if self._popen is None or self._popen.stdin is None:
            raise CodexError("worker is not running")
        blob = json.dumps({"jsonrpc": "2.0", **payload}, separators=(",", ":")) + "\n"
        try:
            with self._write_lock:
                self._popen.stdin.write(blob.encode("utf-8"))
                self._popen.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise CodexError(f"worker stdin is gone: {exc}") from exc

    def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 60.0) -> dict[str, Any]:
        """Call a method and wait for its reply, or raise."""
        with self._id_lock:
            self._next_id += 1
            request_id = self._next_id
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = waiter
        self._send({"id": request_id, "method": method, "params": params or {}})
        try:
            message = waiter.get(timeout=timeout)
        except queue.Empty as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise CodexTimeout(f"{method} did not answer within {timeout}s") from exc
        if "error" in message:
            detail = message["error"]
            raise CodexError(f"{method} failed: {detail.get('message', detail)}")
        return message.get("result") or {}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"method": method, "params": params or {}})

    def drain_notifications(self) -> Iterator[tuple[str, dict[str, Any]]]:
        """Everything received since the last drain, without blocking."""
        while True:
            try:
                yield self._notifications.get_nowait()
            except queue.Empty:
                return

    @property
    def stderr_tail(self) -> list[str]:
        return list(self._stderr_tail)

    # ---- the supervised turn -------------------------------------------

    def handshake(self, *, timeout: float = 60.0) -> dict[str, Any]:
        """initialize, then the ``initialized`` notification the server expects.

        The notification is not optional. Without it the server accepts requests
        and never finishes starting, which presents as a hang rather than an
        error — so it is sent here, immediately, rather than left to a caller to
        remember.
        """
        result = self.request(
            "initialize",
            {
                "clientInfo": {"name": self.client_name, "title": self.client_name, "version": self.version},
                "capabilities": {"experimentalApi": True},
            },
            timeout=timeout,
        )
        self.notify("initialized", {})
        return result

    def start_thread(
        self,
        *,
        cwd: str | os.PathLike[str],
        sandbox: str = "readOnly",
        approval_policy: str = "never",
        network_access: bool = False,
        writable_roots: list[str] | None = None,
        ephemeral: bool = True,
        skip_git_repo_check: bool = True,
        dynamic_tools: list[dict[str, Any]] | None = None,
        config: dict[str, Any] | None = None,
        developer_instructions: str | None = None,
        timeout: float = 60.0,
    ) -> str:
        """Open a thread and return its id.

        ``approval_policy="never"`` does NOT mean "allow everything": it means
        Codex will not stop to ask, and whatever it may do is bounded by the
        sandbox instead. Bothy's default sandbox is read-only, so the default
        posture is a worker that can look and think but not change anything
        until a caller deliberately widens it.
        """
        policy: dict[str, Any] = {"type": sandbox}
        if sandbox == "readOnly":
            policy["networkAccess"] = network_access
        elif sandbox == "workspaceWrite":
            policy["networkAccess"] = network_access
            policy["writableRoots"] = writable_roots or [str(cwd)]
        params: dict[str, Any] = {
            "cwd": str(cwd),
            "sandboxPolicy": policy,
            "approvalPolicy": approval_policy,
            "skipGitRepoCheck": skip_git_repo_check,
            "ephemeral": ephemeral,
        }
        # Declared here rather than written to a file: capability becomes a
        # property of the JOB, not of the installation.
        if dynamic_tools:
            params["dynamicTools"] = dynamic_tools
        if config:
            params["config"] = config
        if developer_instructions:
            params["developerInstructions"] = developer_instructions
        result = self.request("thread/start", params, timeout=timeout)
        thread = result.get("thread") or {}
        thread_id = thread.get("id")
        if not thread_id:
            raise CodexError(f"thread/start returned no thread id: {result!r}")
        return str(thread_id)

    def _interrupt(self, reason: StopReason) -> None:
        """Ask the turn loop to stop, including from a reader callback.

        The loop sends the RPC: waiting for its reply on the reader thread
        would prevent that very thread from delivering the reply.
        The first cause wins so a later deadline never hides a human hold.
        """
        with self._interrupt_lock:
            if self._stop_reason is None:
                self._stop_reason = reason

    def run_turn(
        self,
        *,
        thread_id: str,
        text: str,
        wall_clock_seconds: float = 900.0,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
        request_timeout: float = 60.0,
        max_tokens: int | None = None,
        should_stop: Callable[[], StopReason | None] | None = None,
        interrupt_grace_seconds: float = 1.0,
        on_started: Callable[[str | None], None] | None = None,
    ) -> TurnOutcome:
        """Run one turn under a wall clock, interrupting it if it overruns.

        The deadline is monotonic, so an NTP correction mid-turn cannot end it
        early or extend it. On overrun the turn is interrupted rather than the
        process killed, which gives Codex the chance to close its files and lets
        us record a real outcome instead of a silence.

        ``on_usage`` is called with each cumulative usage total as it arrives.
        That is what lets the budget ledger see an overrun WHILE it is happening
        instead of at settle time, when it is too late to squeeze anyone else.
        """
        outcome = TurnOutcome(status="failed", thread_id=thread_id)
        started = self.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text}]},
                               timeout=request_timeout)
        turn = started.get("turn") or {}
        outcome.turn_id = turn.get("id")
        if on_started is not None:
            on_started(outcome.turn_id)
        item_starts: dict[str, str] = {}
        completed_items: set[str] = set()

        def collect_item(raw: dict[str, Any], observed_at: str | None = None) -> None:
            # The terminal turn also carries items. Reconcile it with streamed
            # completions so a message is neither lost nor printed twice.
            item = dict(raw)
            item_id = item.get("id")
            if item_id is not None:
                key = str(item_id)
                if key in completed_items:
                    return
                completed_items.add(key)
            item["end"] = observed_at or clock.iso()
            item["start"] = item_starts.pop(str(item_id), item["end"])
            if len(outcome.items) < 200:
                outcome.items.append(item)
            if item.get("type") in {"agentMessage", "agent_message"} and item.get("text"):
                outcome.messages.append(str(item["text"]))

        deadline = clock.monotonic() + wall_clock_seconds
        self._hold_limit = deadline + self.question_hold_seconds
        previous_tick = deadline - wall_clock_seconds
        interrupted = False

        while True:
            now = clock.monotonic()
            with self._holds_lock:
                held = [p for p in self.interrupts.values() if p.announced]
                if held and not interrupted:
                    began = min(p.started for p in held)
                    extension = min(max(0, now - max(previous_tick, began)),
                                    self.question_hold_seconds - self._hold_used)
                    self._hold_used += extension
                    deadline += extension
            previous_tick = now
            remaining = deadline - now
            if not interrupted:
                reason = should_stop() if should_stop is not None else None
                self._tick_interrupts(stopping=(reason is not None or self._stop_reason is not None or remaining <= 0))
            if not interrupted:
                if reason is not None:
                    self._interrupt(reason)
                if remaining <= 0:
                    self._interrupt("wall_clock")
            if self._stop_reason is not None and not interrupted:
                self._tick_interrupts(stopping=True)
                interrupted = True
                outcome.status = "interrupted"
                outcome.stop_reason = self._stop_reason
                outcome.error = (f"wall clock of {wall_clock_seconds:.0f}s exceeded"
                                 if outcome.stop_reason == "wall_clock"
                                 else f"turn stopped: {outcome.stop_reason}")
                grace = (min(interrupt_grace_seconds, request_timeout)
                         if outcome.stop_reason == "cancelled" else min(30.0, request_timeout))
                deadline = clock.monotonic() + grace
                try:
                    self.request(
                        "turn/interrupt",
                        {"threadId": thread_id, "turnId": outcome.turn_id},
                        timeout=grace,
                    )
                except CodexError:
                    # Interrupt is best effort; the caller kills the group next.
                    break
                # Give the server a moment to emit its terminal event.
                if outcome.stop_reason != "cancelled":
                    deadline = clock.monotonic() + grace
                continue
            if interrupted and remaining <= 0:
                break

            try:
                method, params = self._notifications.get(timeout=max(0.001, min(remaining, 0.1)))
            except queue.Empty:
                if self._closed.is_set():
                    outcome.error = outcome.error or "app-server exited before the turn completed"
                    outcome.status = "interrupted" if interrupted else "failed"
                    break
                continue

            if method == "thread/tokenUsage/updated":
                total = ((params.get("tokenUsage") or {}).get("total")) or {}
                outcome.usage = total
                if on_usage is not None:
                    try:
                        on_usage(total)
                    except Exception:  # noqa: BLE001 - metering must not end the turn
                        pass
                token_total = total.get("totalTokens", total.get("total_tokens"))
                if token_total is None:
                    token_total = (int(total.get("inputTokens") or total.get("input_tokens") or 0)
                                   + int(total.get("outputTokens") or total.get("output_tokens") or 0))
                if max_tokens is not None and int(token_total) >= max_tokens:
                    self._interrupt("token_cap")
            elif method == "account/rateLimits/updated":
                outcome.rate_limits = params.get("rateLimits") or params
            elif method == "item/started":
                item = params.get("item") or {}
                if len(item_starts) < 200:
                    item_starts[str(item.get("id"))] = params.get("observed_at") or clock.iso()
            elif method == "item/completed":
                collect_item(params.get("item") or {}, params.get("observed_at"))
            elif method == "error":
                detail = params.get("error") or {}
                outcome.error = str(detail.get("message") or detail)
                outcome.error_code = detail.get("codexErrorInfo")
            elif method == "turn/completed":
                finished = params.get("turn") or {}
                for item in finished.get("items") or []:
                    collect_item(item)
                status = str(finished.get("status") or "completed")
                # A turn we interrupted reports as interrupted; keep our reason.
                outcome.status = "interrupted" if interrupted else status
                if finished.get("error") and not interrupted:
                    outcome.error = str(finished["error"])
                break

        # Questions survive on the outcome whether answered or expired.
        self._tick_interrupts(stopping=True)
        outcome.questions = list(self.questions)
        return outcome


def probe_rate_limits(
    *,
    codex_home: str | os.PathLike[str],
    binary: str = "codex",
    env: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Read the account's rate-limit position without spending a turn.

    Subscription mode needs to know how much of the window is gone BEFORE it
    admits a run, and the authority on that is Codex, not us. A handshake plus
    ``account/rateLimits/read`` costs a process spawn and no tokens, so the gate
    can be honest rather than optimistic.

    Returns ``{}`` when the position cannot be read. The caller must treat that
    as unknown — not as zero, which would admit everything.
    """
    client = CodexClient(run_id="probe", codex_home=codex_home, binary=binary, env=env)
    try:
        client.start()
        client.handshake(timeout=timeout)
        return client.request("account/rateLimits/read", {}, timeout=timeout) or {}
    except CodexError:
        return {}
    finally:
        client.close(grace_seconds=5)


def used_percent(rate_limits: dict[str, Any]) -> float | None:
    """Pull the primary window's usedPercent out of whatever shape arrived."""
    block = rate_limits.get("rateLimits") if "rateLimits" in rate_limits else rate_limits
    primary = (block or {}).get("primary") or {}
    value = primary.get("usedPercent", primary.get("used_percent"))
    return float(value) if value is not None else None


def resets_at(rate_limits: dict[str, Any]) -> str | None:
    """The primary window's reset time, converted from epoch to ISO-8601."""
    block = rate_limits.get("rateLimits") if "rateLimits" in rate_limits else rate_limits
    primary = (block or {}).get("primary") or {}
    moment = clock.from_epoch(primary.get("resetsAt", primary.get("resets_at")))
    return clock.iso(moment) if moment else None
