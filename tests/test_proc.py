"""Process groups: the unit of life, because a worker is a tree."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from bothy.proc import ProcessRegistry, Worker, group_alive, group_members, kill_group, spawn_group


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
