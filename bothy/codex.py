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
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Iterator

from . import clock, proc
from .capability import ToolRegistry

__all__ = [
    "CodexError",
    "CodexTimeout",
    "ApprovalDecision",
    "TurnOutcome",
    "CodexClient",
    "default_approval",
]


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


def default_approval(method: str, params: dict[str, Any]) -> ApprovalDecision:
    """Deny by default, and say so.

    A harness running unattended on someone else's machine has no business
    approving anything it was not explicitly configured to approve. The safe
    default is to decline and let the turn continue, so the transcript records
    a refusal rather than a silent success.
    """
    return ApprovalDecision("decline", reason=f"no approval policy configured for {method}")


# --------------------------------------------------------------------------
# outcome


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
        tools: "ToolRegistry | None" = None,
    ) -> None:
        self.run_id = run_id
        self.codex_home = Path(codex_home)
        self.binary = binary
        self.client_name = client_name
        self.version = version
        self._env = env
        self._on_event = on_event or (lambda method, params: None)
        self._approval = approval
        self._tools = tools

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
        try:
            self._on_event(method, params)
        except Exception:  # noqa: BLE001
            pass
        if method == "item/tool/call":
            # A tool Bothy declared on thread/start. The handler runs inside this
            # process, which is the point: it can touch the ledger, the checklist
            # and the audit log directly, with no subprocess, port or credential.
            self._handle_tool_call(message, params)
        elif "requestApproval" in method or method.endswith("/requestUserInput"):
            verdict = self._approval(method, params)
            self._send({"id": message["id"], "result": {"decision": verdict.decision}})
        else:
            # Unknown request: refuse explicitly rather than hanging the turn.
            self._send(
                {
                    "id": message["id"],
                    "error": {"code": -32601, "message": f"{self.client_name} does not implement {method}"},
                }
            )

    def _handle_tool_call(self, message: dict[str, Any], params: dict[str, Any]) -> None:
        """Answer a dynamic tool call. Never raises, always replies.

        A tool that fails answers ``success: false`` with the reason as text, so
        the model can read what went wrong and carry on. Leaving the request
        unanswered would hang the turn until its wall clock, which turns a small
        tool bug into a lost run.
        """
        name = str(params.get("tool") or "")
        if self._tools is None:
            ok, text = False, f"{name} is not available to this run"
        else:
            ok, text = self._tools.call(name, params.get("arguments"))
        self._send({
            "id": message["id"],
            "result": {"success": ok, "contentItems": [{"type": "inputText", "text": text}]},
        })

    def _send(self, payload: dict[str, Any]) -> None:
        if self._popen is None or self._popen.stdin is None:
            raise CodexError("worker is not running")
        blob = json.dumps({"jsonrpc": "2.0", **payload}, separators=(",", ":")) + "\n"
        try:
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

    def run_turn(
        self,
        *,
        thread_id: str,
        text: str,
        wall_clock_seconds: float = 900.0,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
        request_timeout: float = 60.0,
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

        deadline = clock.monotonic() + wall_clock_seconds
        interrupted = False

        while True:
            remaining = deadline - clock.monotonic()
            if remaining <= 0 and not interrupted:
                interrupted = True
                outcome.status = "interrupted"
                outcome.error = f"wall clock of {wall_clock_seconds:.0f}s exceeded"
                try:
                    self.request(
                        "turn/interrupt",
                        {"threadId": thread_id, "turnId": outcome.turn_id},
                        timeout=min(30.0, request_timeout),
                    )
                except CodexError:
                    # Interrupt is best effort; the caller kills the group next.
                    break
                # Give the server a moment to emit its terminal event.
                deadline = clock.monotonic() + 30.0
                continue

            try:
                method, params = self._notifications.get(timeout=max(0.1, min(remaining, 1.0)))
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
            elif method == "account/rateLimits/updated":
                outcome.rate_limits = params.get("rateLimits") or params
            elif method == "item/completed":
                item = params.get("item") or {}
                outcome.items.append(item)
                if item.get("type") in {"agentMessage", "agent_message"}:
                    text_value = item.get("text")
                    if text_value:
                        outcome.messages.append(str(text_value))
            elif method == "error":
                detail = params.get("error") or {}
                outcome.error = str(detail.get("message") or detail)
                outcome.error_code = detail.get("codexErrorInfo")
            elif method == "turn/completed":
                finished = params.get("turn") or {}
                status = str(finished.get("status") or "completed")
                # A turn we interrupted reports as interrupted; keep our reason.
                outcome.status = "interrupted" if interrupted else status
                if finished.get("error"):
                    outcome.error = str(finished["error"])
                break

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
