"""The operator's view. Plain text, exit codes that mean something.

``doctor`` exits NON-ZERO when something is wrong and can print JSON, so it can
gate a deploy. That sounds obvious; the closest comparable project has a doctor
with two dozen checks that always returns success and has no machine-readable
output, so nothing can be built on it. A health command that cannot fail is
decoration.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

import os

from . import __version__, clock, codex
from .alert import Alerter, CompositeSink, DiscordWebhookSink, SlackWebhookSink, StderrSink
from .audit import AuditLog, ChainBreak
from .budget import BudgetLedger
from .capability import Profile
from .config import Config, load
from . import install as installer
from .daemon import Daemon
from .lifecycle import EX_CONFIG
from .wake import Route
from .pool import Admission
from .proc import ProcessRegistry
from .runner import Runner
from .schedule import Job, Schedule, ScheduleError
from .tracker import CairnTracker

__all__ = ["main", "build"]


def build(config: Config) -> tuple[Runner, Admission, AuditLog, ProcessRegistry]:
    """Assemble the harness from a config. One place, so every entry point agrees."""
    config.ensure_dirs()
    audit = AuditLog(config.audit_path)
    ledger = BudgetLedger(config.budget_path, config.budget)
    admission = Admission(config.pool, ledger)
    registry = ProcessRegistry(config.registry_path)
    tracker = CairnTracker(
        agent=config.cairn_agent, binary=config.cairn_binary, project=config.cairn_project
    )
    sinks: list[Any] = [StderrSink()]
    if config.discord_webhook_url:
        sinks.append(DiscordWebhookSink(config.discord_webhook_url))
    if config.slack_webhook_url:
        sinks.append(SlackWebhookSink(config.slack_webhook_url))
    alerter = Alerter(CompositeSink(*sinks), host=config.site)
    runner = Runner(config, admission=admission, audit=audit, registry=registry,
                    tracker=tracker, alerter=alerter)
    return runner, admission, audit, registry


def build_routes(config: Config) -> list[Route]:
    """Resolve configured routes, taking each secret from the environment.

    A route whose secret is missing is a startup ERROR, never a warning. Bothy
    will not hold a door open that it meant to lock, because the day that
    warning scrolls past unread is the day it matters.
    """
    routes: list[Route] = []
    for entry in config.routes:
        env_key = entry.get("secret_env")
        if not env_key:
            raise ValueError(f"route {entry.get('path')} has no secret_env; secrets never live in config.json")
        secret = os.environ.get(env_key)
        if not secret:
            raise ValueError(
                f"route {entry.get('path')} expects its secret in ${env_key}, which is unset. "
                "Refusing to listen on an unsigned endpoint."
            )
        routes.append(Route(
            path=entry["path"],
            secret=secret,
            subject_from=entry.get("subject_from"),
            subject_prefix=entry.get("subject_prefix", ""),
            profile=entry.get("profile"),
            public=bool(entry.get("public", False)),
            signature_header=entry.get("signature_header", "X-Webhook-Signature"),
            timestamp_header=entry.get("timestamp_header", "X-Webhook-Timestamp"),
        ))
    return routes


def cmd_serve(args: argparse.Namespace, config: Config) -> int:
    """Run the daemon in the foreground, for a real supervisor to own."""
    runner, admission, audit, registry = build(config)
    try:
        routes = build_routes(config)
    except ValueError as exc:
        print(f"bothy: {exc}", flush=True)
        return EX_CONFIG
    if not routes:
        print("bothy: no routes configured; there would be nothing to wake on", flush=True)
        return EX_CONFIG
    daemon = Daemon(
        config, runner=runner, admission=admission, audit=audit, registry=registry,
        alerter=runner.alerter, routes=routes,
        janitor_interval_seconds=config.janitor_interval_seconds,
    )
    return daemon.run_forever(reap=args.reap)


# --------------------------------------------------------------------------
# commands


def cmd_status(args: argparse.Namespace, config: Config) -> int:
    runner, admission, audit, registry = build(config)
    observed = None
    if config.budget.mode == "subscription" and args.refresh:
        observed, _ = runner.rate_limit_position(refresh=True)
    snapshot = admission.snapshot(observed_used=observed)
    survivors = registry.survivors()

    if args.json:
        print(json.dumps({"site": config.site, "version": __version__,
                          "pool": snapshot, "workers": [w.as_dict() for w in survivors]}, indent=2))
        return 0

    budget = snapshot["budget"]
    print(f"bothy {__version__} — {config.site}")
    print(f"  pool     {snapshot['state']}  {snapshot['in_flight']}/{snapshot['effective_size']} busy "
          f"(max {snapshot['max_workers']}, failure ratio {snapshot['failure_ratio']} over {snapshot['samples']})")
    print(f"  budget   {budget['used']} used + {budget['reserved']} reserved "
          f"of {budget['limit']} {budget['unit']}  ({budget['headroom']} left, {budget['mode']} mode)")
    if snapshot["lanes"]:
        for subject, run_id in sorted(snapshot["lanes"].items()):
            print(f"  lane     {subject} -> {run_id}")
    else:
        print("  lane     nothing in flight")
    print(f"  workers  {len(survivors)} live process group(s)")
    return 0


def cmd_doctor(args: argparse.Namespace, config: Config) -> int:
    """Check the things that would stop Bothy working, and fail if any do."""
    import pathlib
    import subprocess

    runner, admission, audit, registry = build(config)
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str, *, fatal: bool = True) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "fatal": fatal})

    check("state directory writable", config.state_dir.exists() and
          __import__("os").access(config.state_dir, __import__("os").W_OK), str(config.state_dir))

    try:
        count = audit.verify()
        check("audit chain intact", True, f"{count} records verified")
    except ChainBreak as exc:
        check("audit chain intact", False, f"broken at record {exc.sequence}")

    try:
        version = subprocess.run([config.codex_binary, "--version"], capture_output=True, text=True, timeout=20)
        check("codex present", version.returncode == 0, version.stdout.strip() or version.stderr.strip())
    except Exception as exc:  # noqa: BLE001
        check("codex present", False, str(exc))

    tracker = CairnTracker(agent=config.cairn_agent, binary=config.cairn_binary)
    check("cairn reachable", tracker.available(), "memory and task ledger", fatal=False)

    survivors = registry.survivors()
    check("no orphaned workers", not survivors,
          f"{len(survivors)} process group(s) left by a previous instance")

    # The sandbox is the only real boundary against an adversarial model, so a
    # sandbox that cannot start is a finding, not a footnote. This was found the
    # hard way: every model-only test passed and the first run that needed a
    # shell — a heartbeat working through its checklist — discovered it.
    import shutil as _shutil
    bwrap = _shutil.which("bwrap")
    bundled = list(pathlib.Path("/usr/lib/node_modules/@openai").glob("**/codex-resources/bwrap")) \
        if pathlib.Path("/usr/lib/node_modules/@openai").exists() else []
    if config.sandbox != "dangerFullAccess":
        probe = subprocess.run([bwrap or (str(bundled[0]) if bundled else "bwrap"),
                                "--ro-bind", "/", "/", "--unshare-net", "--", "/bin/true"],
                               capture_output=True, text=True, timeout=20) if (bwrap or bundled) else None
        if probe is None:
            check("sandbox can start", False, "no bubblewrap found on PATH or bundled", fatal=False)
        elif probe.returncode == 0:
            check("sandbox can start", True, bwrap or str(bundled[0]))
        else:
            check("sandbox can start", False,
                  (probe.stderr or probe.stdout).strip().splitlines()[-1][:160] +
                  "  — runs needing a shell will fail", fatal=False)

    if config.slack_app_token_env or config.slack_bot_token_env:
        import os as _os
        tokens_present = bool(_os.environ.get(config.slack_app_token_env or "")) and \
                         bool(_os.environ.get(config.slack_bot_token_env or ""))
        check("slack tokens present", tokens_present,
              f"${config.slack_app_token_env}, ${config.slack_bot_token_env}")
        check("slack allowlist set", bool(config.slack_allow_from),
              f"{len(config.slack_allow_from)} speaker(s) permitted")

    check("alert route configured",
          bool(config.discord_webhook_url or config.slack_webhook_url),
          "a failure nobody hears about is not handled", fatal=False)

    if config.budget.mode == "subscription":
        observed, resets = runner.rate_limit_position(refresh=args.refresh)
        check("subscription window readable", observed is not None,
              f"{observed}% used, resets {resets}" if observed is not None else "could not read")

    failed = [c for c in checks if not c["ok"] and c["fatal"]]
    warned = [c for c in checks if not c["ok"] and not c["fatal"]]

    if args.json:
        print(json.dumps({"ok": not failed, "checks": checks}, indent=2))
    else:
        for entry in checks:
            mark = "ok  " if entry["ok"] else ("FAIL" if entry["fatal"] else "warn")
            print(f"  [{mark}] {entry['check']}: {entry['detail']}")
        print(f"\n{len(checks) - len(failed) - len(warned)} passed, {len(warned)} warned, {len(failed)} failed")
    return 1 if failed else 0


def cmd_run(args: argparse.Namespace, config: Config) -> int:
    runner, _, _, _ = build(config)
    result = runner.run(subject=args.subject, prompt=args.prompt, cairn_ref=args.ref,
                        wall_clock_seconds=args.wall_clock, profile=args.profile)
    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
    else:
        print(f"{result.status}  {result.run_id}  {result.cost:.4f} {result.unit}")
        if result.refusal:
            print(f"  refused at gate '{result.refusal.get('gate')}': {result.refusal.get('reason')}")
        for message in result.messages:
            print(f"  {message}")
        if result.error:
            print(f"  error: {result.error}")
    return 0 if result.ok else 1


def cmd_audit(args: argparse.Namespace, config: Config) -> int:
    audit = AuditLog(config.audit_path)
    if args.verify:
        try:
            print(f"chain intact: {audit.verify()} records")
            return 0
        except ChainBreak as exc:
            print(f"CHAIN BROKEN at record {exc.sequence} in {exc.path}", file=sys.stderr)
            return 1
    for record in audit.records():
        if args.run and record.get("run_id") != args.run:
            continue
        if args.json:
            print(json.dumps(record))
        else:
            print(f"{record['ts']}  {record['kind']:8} {record['action']:16} {record['status']:8} "
                  f"{record.get('subject') or '-':24} {record.get('run_id') or '-'}")
    return 0


def cmd_schedule(args: argparse.Namespace, config: Config) -> int:
    """List, add or remove scheduled jobs."""
    config.ensure_dirs()
    schedule = Schedule(config.state_dir / "jobs.json")

    if args.action == "list":
        jobs = schedule.jobs()
        if args.json:
            print(json.dumps([job.as_dict() for job in jobs], indent=2))
            return 0
        if not jobs:
            print("no scheduled jobs")
            return 0
        for job in sorted(jobs, key=lambda j: j.next_due_at or ""):
            flags = []
            if job.heartbeat:
                flags.append("heartbeat")
            if job.profile:
                flags.append(job.profile)
            if not job.enabled:
                flags.append("disabled")
            if job.misfires:
                flags.append(f"{job.misfires} missed")
            if job.active_hours:
                flags.append(f"{job.active_hours[0]:02d}-{job.active_hours[1]:02d} {job.tz}")
            print(f"  {job.id:16} {job.kind:6} {job.spec:18} next {job.next_due_at or 'never'}"
                  f"{('  [' + ', '.join(flags) + ']') if flags else ''}")
        return 0

    if args.action == "remove":
        print("removed" if schedule.remove(args.id) else f"no job called {args.id}")
        return 0

    # add
    try:
        job = schedule.put(Job(
            id=args.id, kind=args.kind, spec=args.spec, prompt=args.prompt,
            subject=args.subject, tz=args.tz, heartbeat=args.heartbeat, profile=args.profile,
            min_spacing_seconds=args.min_spacing,
            active_hours=[args.active_from, args.active_to] if args.active_from is not None else None,
        ))
    except ScheduleError as exc:
        # Validated now, at the moment a human can fix it, rather than at 3am
        # when it was supposed to fire.
        print(f"bothy: {exc}", flush=True)
        return EX_CONFIG
    print(f"{job.id}: next {job.next_due_at}")
    return 0


def cmd_checklist(args: argparse.Namespace, config: Config) -> int:
    """Show or replace the standing checklist a heartbeat carries."""
    config.ensure_dirs()
    path = config.state_dir / "checklist.md"
    if args.set is not None:
        text = sys.stdin.read() if args.set == "-" else args.set
        path.write_text(text.strip() + "\n", encoding="utf-8")
        print(f"checklist written to {path} ({len(text.strip())} chars)")
        return 0
    if not path.exists():
        print(f"no checklist yet — write one with: bothy checklist --set - < notes.md\n({path})")
        return 0
    print(path.read_text(encoding="utf-8"), end="")
    return 0


def cmd_profiles(args: argparse.Namespace, config: Config) -> int:
    """Show what each capability profile would give a worker."""
    if args.json:
        print(json.dumps(config.profiles, indent=2))
        return 0
    if not config.profiles:
        print("no profiles configured — every run gets Codex built-ins only")
        return 0
    for name in sorted(config.profiles):
        profile = Profile.from_dict(name, config.profiles[name])
        marker = "  (default)" if name == config.default_profile else ""
        print(f"  {name}{marker}\n      {profile.summary()}")
        for server, table in sorted(profile.mcp_servers.items()):
            where = table.get("command") or table.get("url") or "?"
            print(f"      mcp {server}: {where}")
    return 0


def cmd_install(args: argparse.Namespace, config: Config) -> int:
    """Install Bothy as a service. Headless, idempotent, non-zero on failure.

    No prompts and no TTY reads, so it works the same over ssh, in a script, and
    from a deployment tool. Everything it would do can be printed first with
    --dry-run, because the first thing anyone sensible does with an installer
    aimed at their own machine is ask what it is about to touch.
    """
    import os as _os
    import subprocess as _sub

    repo = pathlib.Path(__file__).resolve().parent.parent
    target = installer.plan(site=config.site, home=config.home, repo=repo,
                            user=args.user, kind=args.kind)
    files = {
        target.wrapper_path: (installer.render_wrapper(target), 0o755),
        target.unit_path: (
            installer.render_launchd(target) if target.kind == installer.LAUNCHD
            else installer.render_systemd(target), 0o644),
    }
    if target.kind == installer.LAUNCHD:
        files[pathlib.Path("/etc/newsyslog.d/bothy.conf")] = (installer.render_newsyslog(target), 0o644)

    commands = installer.activate(target)
    serve = installer.render_serve_command(config.port)

    if args.dry_run:
        print(f"would install {target.label} ({target.kind}) for user {target.user}\n")
        for path, (_, mode) in files.items():
            print(f"  write   {path}  mode {mode:o}")
        print(f"  mkdir   {target.log_dir}")
        print(f"  ensure  {target.env_path}  mode 0600  (secrets; never in the unit)")
        for command in commands:
            print(f"  run     {command}")
        print(f"  run     {' '.join(serve)}")
        if args.print_files:
            for path, (body, _) in files.items():
                print(f"\n----- {path} -----\n{body}")
        return 0

    if _os.geteuid() != 0 and target.unit_path.is_absolute() and str(target.unit_path).startswith(("/Library", "/etc")):
        print(f"bothy: installing to {target.unit_path} needs root. Re-run with sudo, "
              f"or use --dry-run --print-files to review what it would write.", flush=True)
        return EX_CONFIG

    # The service does not run as root, so anything it must READ or WRITE has to
    # belong to it. Found the hard way on the first real install: the secrets
    # file was created 0600 root:root and the daemon could not read its own
    # environment, failing with a bare "Permission denied" two layers below
    # anything that mentions Bothy.
    import pwd as _pwd
    try:
        entry = _pwd.getpwnam(target.user)
        owner = (entry.pw_uid, entry.pw_gid)
    except KeyError:
        print(f"bothy: no such user {target.user!r}", flush=True)
        return EX_CONFIG

    try:
        target.log_dir.mkdir(parents=True, exist_ok=True)
        _os.chown(target.log_dir, *owner)
        config.ensure_dirs()
        for path, (body, mode) in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            _os.chmod(path, mode)
            print(f"  wrote {path}")
        if not target.env_path.exists():
            # Created empty and locked down, so there is somewhere obvious to put
            # secrets and no excuse to put them in the world-readable unit.
            target.env_path.parent.mkdir(parents=True, exist_ok=True)
            target.env_path.write_text("# Bothy secrets. Mode 0600. KEY=value per line.\n", encoding="utf-8")
            _os.chown(target.env_path, *owner)
            _os.chmod(target.env_path, 0o600)
            print(f"  created {target.env_path} (mode 0600, owned by {target.user})")
        else:
            # An existing file might predate this fix, or have been placed by
            # hand. Make it readable by the service either way, without
            # touching its contents.
            _os.chown(target.env_path, *owner)
            _os.chmod(target.env_path, 0o600)
    except OSError as exc:
        print(f"bothy: install failed: {exc}", flush=True)
        return 1

    for command in commands:
        result = _sub.run(command, shell=True, capture_output=True, text=True)  # noqa: S602
        status = "ok" if result.returncode == 0 else f"exit {result.returncode}"
        print(f"  {command}  -> {status}")
        if result.returncode != 0 and "|| true" not in command:
            print(f"    {(result.stderr or result.stdout).strip()[:300]}")
            return 1

    if args.serve:
        result = _sub.run(serve, capture_output=True, text=True)
        print(f"  {' '.join(serve)}  -> {'ok' if result.returncode == 0 else (result.stderr or '').strip()[:200]}")
    else:
        print(f"\n  expose it to the tailnet when ready:\n    {' '.join(serve)}")

    print(f"\ninstalled {target.label}. Next: put secrets in {target.env_path}, then 'bothy doctor'.")
    return 0


def cmd_uninstall(args: argparse.Namespace, config: Config) -> int:
    """Remove what install added. Leaves state and secrets alone unless asked."""
    import os as _os
    import subprocess as _sub

    repo = pathlib.Path(__file__).resolve().parent.parent
    target = installer.plan(site=config.site, home=config.home, repo=repo, user=args.user, kind=args.kind)
    for command in installer.deactivate(target):
        result = _sub.run(command, shell=True, capture_output=True, text=True)  # noqa: S602
        print(f"  {command}  -> {'ok' if result.returncode == 0 else 'skipped'}")
    for path in (target.unit_path, target.wrapper_path, pathlib.Path("/etc/newsyslog.d/bothy.conf")):
        try:
            if path.exists():
                path.unlink()
                print(f"  removed {path}")
        except OSError as exc:
            print(f"  could not remove {path}: {exc}")
    print(f"\nleft alone: {target.env_path} (secrets) and {config.home} (state).")
    if not args.purge:
        print("pass --purge to remove those too.")
    else:
        import shutil as _shutil
        for path in (target.env_path,):
            with __import__("contextlib").suppress(OSError):
                path.unlink()
                print(f"  removed {path}")
        _shutil.rmtree(config.home, ignore_errors=True)
        print(f"  removed {config.home}")
    return 0


def cmd_reap(args: argparse.Namespace, config: Config) -> int:
    """Kill anything a previous instance left behind, and release what it held.

    Killing the process is only half of recovery. A crashed run still holds its
    budget reservation, and leaving that to expire on its TTL means an hour of
    refusing real work to protect money nobody is spending. Reaping settles it
    in the same pass.
    """
    _, _, audit, registry = build(config)
    ledger = BudgetLedger(config.budget_path, config.budget)
    outcomes = registry.reap()
    released = 0
    for worker, outcome in outcomes:
        settled = ledger.settle_run(worker.run_id)
        released += len(settled)
        freed = f", released {len(settled)} reservation(s)" if settled else ""
        print(f"  {worker.run_id}  pgid {worker.pgid}  {outcome}{freed}")
        audit.append(kind="worker", action="reaped", run_id=worker.run_id,
                     data={"pgid": worker.pgid, "outcome": outcome,
                           "reservations_released": [r.id for r in settled]})
    # Anything left behind by a run the registry never recorded still ages out.
    expired = ledger.expire_stale()
    if expired:
        print(f"  {len(expired)} reservation(s) expired on TTL")
    print(f"{len(outcomes)} recorded worker(s) handled, {released + len(expired)} reservation(s) released")
    return 0


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bothy", description="a small agent harness you can leave somewhere")
    parser.add_argument("--config", default=None, help="path to config.json")
    parser.add_argument("--version", action="version", version=f"bothy {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="what it is doing right now")
    status.add_argument("--json", action="store_true")
    status.add_argument("--refresh", action="store_true", help="re-read the subscription window (spawns a probe)")
    status.set_defaults(func=cmd_status)

    doctor = subparsers.add_parser("doctor", help="check what would stop it working; non-zero if anything would")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--refresh", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    run = subparsers.add_parser("run", help="run one supervised turn now")
    run.add_argument("prompt")
    run.add_argument("--subject", required=True, help="what this is about; one run per subject at a time")
    run.add_argument("--ref", default=None, help="Cairn task reference to claim and annotate")
    run.add_argument("--wall-clock", type=float, default=None)
    run.add_argument("--profile", default=None, help="capability profile; omit for Codex built-ins only")
    run.add_argument("--json", action="store_true")
    run.set_defaults(func=cmd_run)

    audit = subparsers.add_parser("audit", help="read the record")
    audit.add_argument("--run", default=None, help="only this run id")
    audit.add_argument("--verify", action="store_true", help="check the hash chain")
    audit.add_argument("--json", action="store_true")
    audit.set_defaults(func=cmd_audit)

    install_cmd = subparsers.add_parser("install", help="install as a service; headless and idempotent")
    install_cmd.add_argument("--dry-run", action="store_true", help="print what it would do and stop")
    install_cmd.add_argument("--print-files", action="store_true", help="with --dry-run, show file contents")
    install_cmd.add_argument("--kind", choices=["launchd", "systemd"], default=None)
    install_cmd.add_argument("--user", default=None, help="account the service runs as")
    install_cmd.add_argument("--serve", action="store_true", help="also run 'tailscale serve' now")
    install_cmd.set_defaults(func=cmd_install)

    uninstall_cmd = subparsers.add_parser("uninstall", help="remove the service")
    uninstall_cmd.add_argument("--kind", choices=["launchd", "systemd"], default=None)
    uninstall_cmd.add_argument("--user", default=None)
    uninstall_cmd.add_argument("--purge", action="store_true", help="also remove state and the secrets file")
    uninstall_cmd.set_defaults(func=cmd_uninstall)

    profiles = subparsers.add_parser("profiles", help="what each capability profile grants")
    profiles.add_argument("--json", action="store_true")
    profiles.set_defaults(func=cmd_profiles)

    reap = subparsers.add_parser("reap", help="kill workers a previous instance left behind")
    reap.set_defaults(func=cmd_reap)

    schedule = subparsers.add_parser("schedule", help="jobs that fire without being asked")
    schedule.add_argument("action", choices=["list", "add", "remove"])
    schedule.add_argument("id", nargs="?", default=None)
    schedule.add_argument("--kind", choices=["at", "every", "cron"], default="every")
    schedule.add_argument("--spec", default="3600",
                          help="an ISO instant, seconds, or five cron fields")
    schedule.add_argument("--prompt", default="Anything need attention?")
    schedule.add_argument("--subject", default=None, help="lane to occupy; defaults to job:<id>")
    schedule.add_argument("--tz", default="UTC")
    schedule.add_argument("--profile", default=None, help="capability profile for this job")
    schedule.add_argument("--heartbeat", action="store_true",
                          help="carry the standing checklist and honour NO_REPLY")
    schedule.add_argument("--min-spacing", type=float, default=60.0)
    schedule.add_argument("--active-from", type=int, default=None)
    schedule.add_argument("--active-to", type=int, default=None)
    schedule.add_argument("--json", action="store_true")
    schedule.set_defaults(func=cmd_schedule)

    checklist = subparsers.add_parser("checklist", help="the standing checklist a heartbeat carries")
    checklist.add_argument("--set", default=None, help="replace it; '-' reads stdin")
    checklist.set_defaults(func=cmd_checklist)

    serve = subparsers.add_parser("serve", help="run the daemon in the foreground")
    serve.add_argument("--reap", action="store_true",
                       help="kill a previous instance's workers instead of refusing to start")
    serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    config = load(args.config)
    return int(args.func(args, config))


if __name__ == "__main__":
    sys.exit(main())
