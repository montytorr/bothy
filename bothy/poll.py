"""Reaching out on a schedule, so nothing has to reach in.

This is the sealed alternative to a public webhook endpoint. The machine keeps
zero public ingress: nothing listed in certificate transparency logs as a live
endpoint, no unauthenticated request path to defend, no signature verification
to get wrong, and no denial-of-service vector at all. It also survives the
machine being off — events queue at the provider and we catch up, whereas a
missed webhook depends entirely on the sender's retry policy.

The costs are real and should be said plainly: latency measured in seconds to
minutes, API quota, and state to keep.

POLLING IS AT-LEAST-ONCE TOO, and more obviously so. A webhook is redelivered
when an acknowledgement is missed; a poller re-reads an overlapping window every
single time, so duplicates are the NORMAL case rather than the exception. That
makes dedupe on the item's own id load-bearing rather than defensive, and it is
persisted for the same reason the webhook receiver's is: a restart must not
reopen the window.

CONDITIONAL REQUESTS ARE THE WHOLE ECONOMY. With ETag and Last-Modified, an
unchanged poll costs a 304 and no quota at all. A poller that ignores them is
the reason people believe polling is expensive; one that uses them can run every
minute against most APIs for nothing.

A source that fails backs off on its own and never stops the others. Secrets are
named by environment variable and never written to config, the same rule routes
follow.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from . import clock, ids

__all__ = ["Source", "PollState", "Poller", "PollResult", "dig"]

MAX_BODY_BYTES = 8 * 1024 * 1024
DEFAULT_SEEN_CAP = 2000


def dig(value: Any, path: str | None) -> Any:
    """Follow a dotted path into nested data, tolerating absence.

    Deliberately forgiving: an API that changes shape should yield nothing and
    be visible as "no events", not raise inside a polling loop that then stops
    checking everything else.
    """
    if not path:
        return value
    cursor = value
    for part in path.split("."):
        if isinstance(cursor, dict):
            cursor = cursor.get(part)
        elif isinstance(cursor, list) and part.isdigit():
            cursor = cursor[int(part)] if int(part) < len(cursor) else None
        else:
            return None
        if cursor is None:
            return None
    return cursor


@dataclasses.dataclass
class Source:
    """One thing to poll."""

    name: str
    url: str
    interval_seconds: float = 300.0
    method: str = "GET"
    # Header name -> environment variable holding its value. The value itself
    # never appears in config, for the same reason route secrets do not.
    header_env: dict[str, str] = dataclasses.field(default_factory=dict)
    headers: dict[str, str] = dataclasses.field(default_factory=dict)
    items_path: str | None = None       # dotted path to the list of items
    id_path: str = "id"                 # dotted path, within an item, to its id
    subject_from: str | None = None
    subject_prefix: str = ""
    profile: str | None = None
    prompt_template: str | None = None
    conditional: bool = True            # send If-None-Match / If-Modified-Since
    cursor_param: str | None = None     # query param carrying the cursor, if any
    cursor_from: str | None = None      # dotted path, within an item, to the next cursor
    max_items_per_poll: int = 25
    timeout: float = 30.0

    def resolved_headers(self) -> dict[str, str]:
        out = dict(self.headers)
        for name, env_key in self.header_env.items():
            value = os.environ.get(env_key)
            if not value:
                raise KeyError(f"source {self.name}: ${env_key} is unset")
            out[name] = value
        out.setdefault("User-Agent", "bothy/0.1")
        out.setdefault("Accept", "application/json")
        return out

    def subject(self, item: dict[str, Any], fallback: str) -> str:
        value = dig(item, self.subject_from) if self.subject_from else None
        return f"{self.subject_prefix}{value if value else fallback}"

    def item_id(self, item: dict[str, Any]) -> str | None:
        value = dig(item, self.id_path)
        return str(value) if value is not None else None


@dataclasses.dataclass
class PollResult:
    """What one poll did, for the audit log and for an operator."""

    source: str
    status: str                 # ok | unchanged | failed | skipped
    fetched: int = 0
    new: int = 0
    duplicates: int = 0
    http_status: int | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class PollState:
    """Per-source cursors, validators and seen ids, persisted.

    On disk because all three must survive a restart. A forgotten ETag costs
    quota; a forgotten cursor re-reads history; a forgotten set of seen ids
    replays work that was already done, which for an agent harness means
    spending money doing something twice.
    """

    def __init__(self, path: str | os.PathLike[str], *, seen_cap: int = DEFAULT_SEEN_CAP) -> None:
        self.path = Path(path)
        self.seen_cap = seen_cap
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, Any]) -> None:
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

    def get(self, source: str) -> dict[str, Any]:
        entry = self._load().get(source) or {}
        entry.setdefault("seen", [])
        return entry

    def remember(self, source: str, *, etag: str | None = None, modified: str | None = None,
                 cursor: str | None = None, new_ids: Iterable[str] = (),
                 failures: int | None = None, last_status: str | None = None) -> None:
        with self._lock:
            data = self._load()
            entry = data.get(source) or {"seen": []}
            if etag is not None:
                entry["etag"] = etag
            if modified is not None:
                entry["modified"] = modified
            if cursor is not None:
                entry["cursor"] = cursor
            if failures is not None:
                entry["failures"] = failures
            if last_status is not None:
                entry["last_status"] = last_status
            entry["last_polled_at"] = clock.iso()
            seen = list(entry.get("seen") or [])
            for item_id in new_ids:
                if item_id not in seen:
                    seen.append(item_id)
            # Bounded, oldest first. An unbounded set of ids is a slow leak that
            # only shows up on the deployment that has been running longest.
            entry["seen"] = seen[-self.seen_cap:]
            data[source] = entry
            self._save(data)


class Poller:
    """Fetches sources and turns genuinely-new items into wakes."""

    def __init__(self, state: PollState, *, opener: Any = None) -> None:
        self.state = state
        # Injectable so the fetch path can be tested without a network.
        self._open = opener or urllib.request.urlopen

    def _fetch(self, source: Source, entry: dict[str, Any]) -> tuple[int, bytes, dict[str, str]]:
        url = source.url
        if source.cursor_param and entry.get("cursor"):
            joiner = "&" if "?" in url else "?"
            url = f"{url}{joiner}{source.cursor_param}={entry['cursor']}"
        headers = source.resolved_headers()
        if source.conditional:
            # The whole economy of polling. An unchanged resource answers 304
            # and costs no quota on most APIs.
            if entry.get("etag"):
                headers["If-None-Match"] = entry["etag"]
            if entry.get("modified"):
                headers["If-Modified-Since"] = entry["modified"]
        request = urllib.request.Request(url, method=source.method, headers=headers)  # noqa: S310
        try:
            with self._open(request, timeout=source.timeout) as response:  # noqa: S310
                body = response.read(MAX_BODY_BYTES)
                return response.status, body, {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return 304, b"", {k.lower(): v for k, v in (exc.headers or {}).items()}
            raise

    def poll(self, source: Source) -> tuple[PollResult, list[dict[str, Any]]]:
        """Poll one source. Returns the outcome and any wakes to enqueue."""
        entry = self.state.get(source.name)
        result = PollResult(source=source.name, status="ok")
        try:
            status, body, headers = self._fetch(source, entry)
        except KeyError as exc:
            # A missing credential is a configuration problem, not a transient
            # one, and it should be visible rather than retried forever.
            result.status, result.detail = "skipped", str(exc)
            self.state.remember(source.name, last_status="skipped")
            return result, []
        except Exception as exc:  # noqa: BLE001
            failures = int(entry.get("failures", 0)) + 1
            result.status, result.detail = "failed", f"{type(exc).__name__}: {exc}"
            self.state.remember(source.name, failures=failures, last_status="failed")
            return result, []

        result.http_status = status
        if status == 304:
            result.status = "unchanged"
            self.state.remember(source.name, failures=0, last_status="unchanged")
            return result, []

        try:
            payload = json.loads(body) if body else []
        except json.JSONDecodeError as exc:
            result.status, result.detail = "failed", f"response was not JSON: {exc}"
            self.state.remember(source.name, failures=int(entry.get("failures", 0)) + 1, last_status="failed")
            return result, []

        items = dig(payload, source.items_path)
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            items = []
        result.fetched = len(items)

        seen = set(entry.get("seen") or [])
        wakes: list[dict[str, Any]] = []
        new_ids: list[str] = []
        cursor = entry.get("cursor")

        # Oldest first, so a truncated batch leaves the newest for next time
        # rather than stranding the oldest behind a cap forever.
        for item in items[: source.max_items_per_poll]:
            if not isinstance(item, dict):
                continue
            item_id = source.item_id(item)
            if item_id is None:
                # Without a stable id there is no way to avoid replaying it, and
                # replaying work costs money. Counted, not guessed at.
                result.duplicates += 1
                continue
            if item_id in seen:
                result.duplicates += 1
                continue
            new_ids.append(item_id)
            wake_id = ids.wake_id()
            wakes.append({
                "id": wake_id,
                "received_at": clock.iso(),
                "route": f"poll:{source.name}",
                "subject": source.subject(item, wake_id),
                "delivery_key": f"poll:{source.name}:{item_id}",
                "payload": {"item": item, "source": source.name,
                            **({"prompt": source.prompt_template.format(item=json.dumps(item, indent=1)[:4000])}
                               if source.prompt_template else {})},
                "profile": source.profile,
                "processed": False,
            })
            if source.cursor_from:
                nxt = dig(item, source.cursor_from)
                if nxt is not None:
                    cursor = str(nxt)

        result.new = len(wakes)
        self.state.remember(
            source.name,
            etag=headers.get("etag"),
            modified=headers.get("last-modified"),
            cursor=cursor,
            new_ids=new_ids,
            failures=0,
            last_status="ok",
        )
        return result, wakes
