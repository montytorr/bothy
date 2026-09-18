"""Starting once, dying honestly, and telling the supervisor what to do."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bothy.install import LAUNCHD, SYSTEMD, plan, render_funnel_commands, render_launchd, render_systemd, render_wrapper
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


if __name__ == "__main__":
    unittest.main()
