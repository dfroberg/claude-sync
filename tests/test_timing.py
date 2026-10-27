from __future__ import annotations

import time

from tests.harness import SyncTestCase


def timed(fn) -> float:
    t0 = time.monotonic()
    fn()
    return time.monotonic() - t0


class TimingTest(SyncTestCase):
    """Spec test 15 and section 6.14: the hook budgets on a fixture shaped like the live data, git-crypt on.

    The fixture is built once per class (git-crypt encrypts 1350 files). A class-level SyncTestCase holds
    it, so the per-test setUp and tearDown below do nothing and the fixture is not wiped between tests.
    """

    @classmethod
    def setUpClass(cls):
        cls.case = SyncTestCase("setUp")                     # skips the class when git-crypt is missing
        cls.case.setUp()
        cls.addClassCleanup(cls.case.doCleanups)
        cls.key = cls.case.make_fixture()

    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_budgets(self):
        fx = self.case
        a = fx.machine("a")
        env = a.env(CLAUDE_SYNC_KEY=str(self.key))
        timings = {}
        timings["first_pull"] = t = timed(lambda: a.run("pull", env=env))
        self.assertLess(t, 60, timings)                      # first pull, no base
        # the first push after a bulk first pull pays a one-time first-open cost per new file
        # (com.apple.provenance check, about 1.2 ms per file on this Mac, also outside the sandbox);
        # the budget is for the tick push on a warm tree.
        timings["push_first_after_bulk_pull"] = timed(lambda: a.run("push", env=env))
        timings["push_nothing"] = t = timed(lambda: a.run("push", env=env))
        self.assertLess(t, 1, timings)
        timings["pull_nothing"] = t = timed(lambda: a.run("pull", env=env))
        self.assertLess(t, 2, timings)
        fx.remote_commit_n(50)
        timings["pull_50_remote"] = t = timed(lambda: a.run("pull", env=env))
        self.assertIn("50 remote changes, 50 written", a.log_lines()[-1])
        self.assertLess(t, 5, timings)
        tip = fx.remote_tip()
        a.edit_n(50)
        timings["push_50_local"] = t = timed(lambda: a.run("push", env=env))
        self.assertIn(", 50 pushed", a.log_lines()[-1])
        self.assertNotEqual(fx.remote_tip(), tip)
        self.assertLess(t, 10, timings)
        print(f"timings: { {k: round(v, 2) for k, v in timings.items()} }")
