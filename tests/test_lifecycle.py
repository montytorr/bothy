"""Starting once, dying honestly, and telling the supervisor what to do."""

from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bothy.install import (
    LAUNCHAGENT, LAUNCHD, SYSTEMD, activate, agent_path, deactivate, plan, render_funnel_commands,
    render_launchagent, render_launchd, render_systemd, render_wrapper,
)
from bothy.lifecycle import EX_CONFIG, EX_TEMPFAIL, AlreadyRunning, InstanceLock, LifecycleLedger


class InstanceLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "bothy.lock"

    def test_a_second_instance_is_refused_and_named(self) -> None:
        """Two Bothys on one state directory would hand out the same slots twice."""
        first = InstanceLock(self.path).acquire()
        try:
            with self.assertRaises(AlreadyRunning) as caught:
                InstanceLock(self.path).acquire()
            self.assertIn("already holds", str(caught.exception))
        finally:
            first.release()

    def test_releasing_frees_it(self) -> None:
        InstanceLock(self.path).acquire().release()
        InstanceLock(self.path).acquire().release()


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "lifecycle.json"

    def test_a_first_start_reports_nothing(self) -> None:
        self.assertIsNone(LifecycleLedger(self.path).open_run())

    def test_an_unclean_death_is_visible_at_the_next_start(self) -> None:
        LifecycleLedger(self.path).open_run()          # started, never closed
        death = LifecycleLedger(self.path).detect_unclean()
        self.assertIsNotNone(death)
        self.assertIn("never recorded a clean stop", death.summary())

    def test_a_clean_stop_leaves_nothing_to_report(self) -> None:
        ledger = LifecycleLedger(self.path)
        ledger.open_run()
        ledger.close_run(reason="stopped")
        self.assertIsNone(LifecycleLedger(self.path).detect_unclean())

    def test_detection_does_not_claim_the_ledger(self) -> None:
        """A refusal must not manufacture a phantom death for the next start.

        The first version detected and claimed in one step, so a start that was
        correctly refused left the ledger marked running.
        """
        ledger = LifecycleLedger(self.path)
        ledger.open_run()
        ledger.close_run(reason="stopped")
        fresh = LifecycleLedger(self.path)
        fresh.detect_unclean()            # as a start that then refuses would
        self.assertIsNone(LifecycleLedger(self.path).detect_unclean())


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())

    def target(self, kind: str):  # noqa: ANN201
        return plan(site="acme", home=self.home, repo=Path("/opt/bothy"), user="ops", kind=kind)

    def test_the_two_managers_need_opposite_handling_of_exit_78(self) -> None:
        """systemd must SEE 78; launchd cannot express it and needs it translated.

        Translating for both defeats the mechanism: the unit also says
        Restart=always, which restarts on a clean exit too, so systemd
        cheerfully restarted the thing that had just said restarting cannot help.
        """
        launchd_wrapper = render_wrapper(self.target(LAUNCHD))
        systemd_wrapper = render_wrapper(self.target(SYSTEMD))
        self.assertIn("exit 0", launchd_wrapper)
        self.assertIn('"$code" -eq 78', launchd_wrapper)
        self.assertNotIn('"$code" -eq 78', systemd_wrapper)
        self.assertIn("RestartPreventExitStatus=78", render_systemd(self.target(SYSTEMD)))

    def test_the_wrapper_waits_for_tailscaled(self) -> None:
        """launchd has no ordering graph; systemd's After= waits for a unit, not a socket."""
        self.assertIn("tailscaled.sock", render_wrapper(self.target(LAUNCHD)))

    def test_no_secret_reaches_the_service_definition(self) -> None:
        """A launchd plist is world-readable."""
        plist = render_launchd(self.target(LAUNCHD))
        self.assertNotIn("SECRET", plist.upper().replace("SECRETS", ""))
        self.assertIn("bothy.env", render_wrapper(self.target(LAUNCHD)))

    def test_a_launch_daemon_not_a_launch_agent(self) -> None:
        """An agent needs a GUI login, which an unattended mini may not have."""
        target = self.target(LAUNCHD)
        self.assertIn("/Library/LaunchDaemons/", str(target.unit_path))

    def test_funnel_ports_are_enforced_before_anything_is_configured(self) -> None:
        with self.assertRaises(ValueError):
            render_funnel_commands(9000, 8788, ["/hook"])

    def test_bothy_is_never_asked_to_bind_a_privileged_port(self) -> None:
        """tailscaled binds the public port and proxies to an ordinary one."""
        with self.assertRaises(ValueError):
            render_funnel_commands(443, 443, ["/hook"])

    def test_the_funnel_command_mounts_only_named_paths(self) -> None:
        commands = render_funnel_commands(443, 8788, ["/hook/github"])
        joined = " ".join(commands[0])
        self.assertIn("--set-path=/hook/github", joined)
        self.assertIn("http://127.0.0.1:8788", joined)
        self.assertNotIn("--set-path=/ ", joined + " ")

    def test_exit_codes_are_the_documented_ones(self) -> None:
        self.assertEqual((EX_TEMPFAIL, EX_CONFIG), (75, 78))


class LaunchAgentTests(unittest.TestCase):
    """A per-user LaunchAgent, for the machine somebody sits at.

    A LaunchDaemon has no GUI session, so it cannot reach the herdr server a
    person started, the Claude login in their Keychain, or ~/.local/bin. These
    pin what that costs and what it must not cost.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.bothy_home = self.tmp / ".bothy"
        env = mock.patch.dict(os.environ, {"HOME": str(self.tmp), "USER": "cal", "PATH": f"{self.tmp}:/nonexistent/dir"})
        env.start()
        self.addCleanup(env.stop)

    def target(self, **kwargs):  # noqa: ANN201
        return plan(site="mac", home=self.bothy_home, repo=Path("/opt/bothy"), kind=LAUNCHAGENT,
                    python="/usr/bin/python3", **kwargs)

    def test_it_installs_into_the_users_own_account_with_no_root_path(self) -> None:
        target = self.target()
        self.assertEqual(target.unit_path, self.tmp / "Library" / "LaunchAgents" / "dev.bothy.mac.plist")
        for path in (target.unit_path, target.wrapper_path, target.env_path, target.log_dir):
            self.assertTrue(str(path).startswith(str(self.tmp)), f"{path} is not under the user's home")

    def test_the_plist_is_valid_names_no_user_and_carries_the_path_it_was_given(self) -> None:
        target = self.target(path=f"{self.tmp}:/usr/bin")
        loaded = plistlib.loads(render_launchagent(target).encode())
        self.assertNotIn("UserName", loaded, "an agent runs as whoever logged in; a UserName would need root")
        self.assertEqual(loaded["Label"], "dev.bothy.mac")
        self.assertTrue(loaded["EnvironmentVariables"]["PATH"].startswith(f"{self.tmp}:/usr/bin"))
        self.assertEqual(loaded["EnvironmentVariables"]["BOTHY_HOME"], str(self.bothy_home))
        self.assertIs(loaded["KeepAlive"]["SuccessfulExit"], False)

    def test_the_path_is_this_shells_less_what_does_not_exist_plus_the_system_dirs(self) -> None:
        got = agent_path(f"{self.tmp}:/nonexistent/dir:{self.tmp}:/usr/local/bin").split(":")
        self.assertEqual(got.count(str(self.tmp)), 1, "deduplicated")
        self.assertNotIn("/nonexistent/dir", got)
        for system in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
            self.assertIn(system, got)

    def test_it_is_loaded_into_the_users_gui_domain_not_the_system_one(self) -> None:
        target = self.target()
        self.assertEqual(target.uid, os.getuid())
        joined = " ".join(activate(target))
        self.assertIn(f"launchctl bootstrap gui/{os.getuid()} ", joined)
        self.assertNotIn("system/", joined)
        self.assertEqual(deactivate(target),
                         [f"launchctl bootout gui/{os.getuid()}/dev.bothy.mac 2>/dev/null || true"])

    def test_the_wrapper_keeps_the_exit_contract_and_does_not_wait_for_a_socket(self) -> None:
        wrapper = render_wrapper(self.target())
        self.assertIn('"$code" -eq 78', wrapper, "launchd cannot express exit 78, so it is translated")
        self.assertNotIn("tailscaled.sock", wrapper, "an agent on a machine you sit at has nothing to wait for")
        self.assertIn('"/usr/bin/python3" "/opt/bothy/bin/bothy" serve --reap', wrapper,
                      "the interpreter that ran the installer, not whatever PATH finds")
        self.assertIn("bothy.env", wrapper)

    def run_main(self, *argv: str) -> tuple[int, str]:
        from bothy.cli import main

        config = self.tmp / "config.json"
        config.write_text(json.dumps({"site": "mac", "home": str(self.bothy_home)}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            return main(["--config", str(config), *argv]), out.getvalue()

    def test_a_dry_run_writes_nothing_and_shows_the_path_and_the_domain(self) -> None:
        code, output = self.run_main("install", "--kind", "launchagent", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("PATH", output)
        self.assertIn(f"gui/{os.getuid()}", output)
        self.assertFalse((self.tmp / "Library").exists())
        self.assertFalse(self.bothy_home.exists())

    def test_install_writes_the_unit_and_a_private_env_file_and_uninstall_leaves_secrets(self) -> None:
        ran: list[str] = []

        def fake_run(command, **kwargs):  # noqa: ANN001, ANN202
            ran.append(command if isinstance(command, str) else " ".join(command))
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch("subprocess.run", fake_run):
            code, output = self.run_main("install", "--kind", "launchagent")
        self.assertEqual(code, 0, output)
        unit = self.tmp / "Library" / "LaunchAgents" / "dev.bothy.mac.plist"
        wrapper = self.bothy_home / "bothy-run"
        env = self.bothy_home / "bothy.env"
        self.assertTrue(unit.exists() and wrapper.exists() and env.exists())
        self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600, "secrets never world-readable")
        self.assertEqual(stat.S_IMODE(wrapper.stat().st_mode), 0o755)
        self.assertTrue(any("launchctl bootstrap gui/" in command for command in ran))

        with mock.patch("subprocess.run", fake_run):
            code, _ = self.run_main("uninstall", "--kind", "launchagent")
        self.assertEqual(code, 0)
        self.assertFalse(unit.exists())
        self.assertFalse(wrapper.exists())
        self.assertTrue(env.exists(), "secrets and state are left unless asked")
        self.assertTrue(any("launchctl bootout gui/" in command for command in ran))

    def test_uninstalling_an_agent_never_reaches_for_a_system_file(self) -> None:
        """The daemon's newsyslog entry is root's. An agent has no business with it."""
        removed: list[str] = []

        def fake_unlink(path, *args, **kwargs):  # noqa: ANN001, ANN202
            removed.append(str(path))

        with mock.patch.object(Path, "exists", lambda self: True), \
                mock.patch.object(Path, "unlink", fake_unlink), \
                mock.patch("subprocess.run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")):
            code, _ = self.run_main("uninstall", "--kind", "launchagent")
        self.assertEqual(code, 0)
        self.assertTrue(removed, "it did remove its own files")
        self.assertFalse([path for path in removed if path.startswith("/etc/")], removed)

    def test_root_is_refused_because_it_would_install_into_the_wrong_account(self) -> None:
        with mock.patch("os.geteuid", return_value=0):
            code, output = self.run_main("install", "--kind", "launchagent")
        self.assertEqual(code, 78)
        self.assertIn("without sudo", output)
        self.assertFalse((self.tmp / "Library").exists())


class ServeTests(unittest.TestCase):
    """A schedule is a wake source. A cron-only install is a real deployment."""

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())

    def serve(self, **settings: object) -> int:
        from bothy.cli import cmd_serve
        from bothy.config import Config

        import argparse

        config = Config(home=self.home, **settings)
        with mock.patch("bothy.daemon.Daemon.run_forever", return_value=0), \
                contextlib.redirect_stdout(io.StringIO()):
            return cmd_serve(argparse.Namespace(reap=False), config)

    def test_nothing_that_could_wake_it_is_a_configuration_error(self) -> None:
        self.assertEqual(self.serve(), EX_CONFIG)

    def test_a_scheduled_job_alone_is_enough_to_start(self) -> None:
        from bothy.schedule import Job, Schedule

        (self.home / "state").mkdir(parents=True)
        Schedule(self.home / "state" / "jobs.json").put(
            Job(id="n", kind="cron", spec="0 2 * * *", drove={"action": "plan", "next": "DROVE"}))
        self.assertEqual(self.serve(), 0)

    def test_forwarding_drove_attention_to_a_webhook_alone_is_enough_to_start(self) -> None:
        self.assertEqual(self.serve(drove_attention_interval_seconds=30.0,
                                    discord_webhook_url="http://127.0.0.1:1/x"), 0)
        self.assertEqual(self.serve(drove_attention_interval_seconds=30.0,
                                    slack_webhook_url="http://127.0.0.1:1/x"), 0)

    def test_attention_with_nowhere_to_tell_it_is_still_nothing_to_wake_on(self) -> None:
        self.assertEqual(self.serve(drove_attention_interval_seconds=30.0), EX_CONFIG)

    def test_a_webhook_without_attention_is_still_nothing_to_wake_on(self) -> None:
        self.assertEqual(self.serve(discord_webhook_url="http://127.0.0.1:1/x"), EX_CONFIG)


if __name__ == "__main__":
    unittest.main()
