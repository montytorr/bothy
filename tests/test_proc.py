"""Process groups: the unit of life, because a worker is a tree."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from bothy.proc import (
    ProcessRegistry, Worker, boot_id, _parse_ps, _start_time, group_alive, group_members, kill_group,
    spawn_group,
)


class ProcessGroupTests(unittest.TestCase):
    def test_a_whole_tree_is_killed_not_just_the_parent(self) -> None:
        """Signalling the shim leaves the rest alive. That is how orphans accumulate."""
        child = spawn_group(["/bin/sh", "-c", "sleep 60 & sleep 60"])
        time.sleep(0.4)
        self.assertGreaterEqual(len(group_members(child.pid)), 2, "it really is a tree")
        self.assertEqual(kill_group(child.pid, grace_seconds=5), "terminated")
        child.wait()
        self.assertEqual(group_members(child.pid), [])

    def test_a_zombie_is_not_mistaken_for_a_live_process(self) -> None:
        """killpg(pgid, 0) succeeds on an unreaped zombie.

        The first version of kill_group sat out its entire grace period against
        a corpse and then reported "unkillable".
        """
        child = spawn_group(["/bin/sh", "-c", "exit 0"])
        time.sleep(0.3)
        # Deliberately NOT waited on, so the leader is a zombie right now.
        self.assertFalse(group_alive(child.pid), "a zombie is dead for our purposes")
        self.assertEqual(kill_group(child.pid, grace_seconds=2), "already-gone")
        child.wait()

    def test_kill_is_idempotent(self) -> None:
        child = spawn_group(["/bin/sh", "-c", "sleep 30"])
        time.sleep(0.3)
        kill_group(child.pid, grace_seconds=5)
        child.wait()
        self.assertEqual(kill_group(child.pid, grace_seconds=1), "already-gone")


class WithoutProcTests(unittest.TestCase):
    """macOS has no /proc, so the same questions are answered from ps.

    Until it could, group_members returned [] for every group on a Mac: a
    whole-tree kill reported "already-gone" without signalling anything and a
    restart could never find its orphans. The two tests in ProcessGroupTests and
    RegistryTests that exercise that failed there. These force the ps path so it
    is exercised on Linux as well.
    """

    def setUp(self) -> None:
        patcher = mock.patch("bothy.proc._has_proc", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_whole_tree_is_found_and_killed_from_ps_alone(self) -> None:
        child = spawn_group(["/bin/sh", "-c", "sleep 60 & sleep 60"])
        time.sleep(0.4)
        self.assertGreaterEqual(len(group_members(child.pid)), 2)
        self.assertEqual(kill_group(child.pid, grace_seconds=5), "terminated")
        child.wait()
        self.assertEqual(group_members(child.pid), [])

    def test_a_zombie_is_dead_for_our_purposes_here_too(self) -> None:
        child = spawn_group(["/bin/sh", "-c", "exit 0"])
        time.sleep(0.3)
        self.assertFalse(group_alive(child.pid))
        child.wait()

    def test_ps_rows_are_read_by_name_and_junk_is_skipped(self) -> None:
        rows = _parse_ps("  100   100 Ss\n  101   100 S+\n  102   100 Z\nheader line\n  x  y Z\n 103 104\n")
        self.assertEqual(rows, [(100, 100, False), (101, 100, False), (102, 100, True)])

    def test_a_start_time_and_a_boot_identify_this_machine_and_this_process(self) -> None:
        self.assertIsNotNone(_start_time(os.getpid()))
        self.assertEqual(_start_time(os.getpid()), _start_time(os.getpid()))
        self.assertIsNone(_start_time(2 ** 22 + 12345), "no such process")
        self.assertTrue(boot_id())

    def test_an_unreadable_table_is_not_nobody_being_there(self) -> None:
        """"I cannot tell" is a third state: fall back to a signal probe, never to "gone"."""
        child = spawn_group(["/bin/sh", "-c", "sleep 30"])
        time.sleep(0.3)
        with mock.patch("bothy.proc._process_table", return_value=None):
            self.assertTrue(group_alive(child.pid))
            self.assertFalse(group_alive(2 ** 22 + 12345), "no such group answers no, not maybe")
        kill_group(child.pid, grace_seconds=5)
        child.wait()


class BootIdTests(unittest.TestCase):
    def test_the_ledger_and_the_process_registry_agree_on_which_boot_this_is(self) -> None:
        """They were two copies of a /proc read, and on macOS the ledger's returned None.

        So a reboot could not be told from a crash there: both read "same boot".
        """
        from bothy.lifecycle import LifecycleLedger

        self.assertTrue(boot_id())
        self.assertEqual(LifecycleLedger._boot_id(), boot_id())


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.registry = ProcessRegistry(self.dir / "workers.json")

    def test_a_survivor_is_found_and_reaped_on_the_next_start(self) -> None:
        child = spawn_group(["/bin/sh", "-c", "sleep 60 & sleep 60"])
        self.registry.register("run_orphan", child, ["sleep"])
        time.sleep(0.3)
        self.assertEqual([w.run_id for w in self.registry.survivors()], ["run_orphan"])

        fresh = ProcessRegistry(self.dir / "workers.json")   # as a restart would see it
        outcomes = fresh.reap(grace_seconds=5)
        child.wait()
        self.assertEqual([outcome for _, outcome in outcomes], ["terminated"])
        self.assertEqual(fresh.survivors(), [])

    def test_a_recycled_pid_is_never_signalled(self) -> None:
        """A pid alone is not an identity; the kernel recycles them."""
        impostor = Worker(run_id="r", pid=1, pgid=1, argv=[],
                          started_at="2020-01-01T00:00:00+00:00",
                          start_ticks="999999999", boot_id="a-different-boot")
        self.assertFalse(impostor.still_ours())

    def test_the_registry_over_states_rather_than_under_states(self) -> None:
        """Over-stating costs a liveness check; under-stating costs an orphan."""
        child = spawn_group(["/bin/sh", "-c", "exit 0"])
        self.registry.register("run_done", child, ["true"])
        child.wait()
        self.assertEqual(len(self.registry.workers()), 1, "still recorded")
        self.assertEqual(self.registry.survivors(), [], "but known not to be alive")


if __name__ == "__main__":
    unittest.main()
