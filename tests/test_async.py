"""Critical behaviour: reliable asynchronous processing (failure, duplicates, delay, crash)."""
from datetime import timedelta

from app import worker
from app.common import now_dt, to_iso

from .base import ApiTestCase


class Outbox(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._orig = dict(worker.HANDLERS)

    def tearDown(self):
        worker.HANDLERS.clear()
        worker.HANDLERS.update(self._orig)
        super().tearDown()

    def notifs(self, email):
        return self.db.execute("SELECT * FROM notifications WHERE user_id=?", (self.uid[email],)).fetchall()

    def test_job_is_created_atomically_with_the_action_and_user_is_not_blocked_on_it(self):
        item = self.mk(who="alice@t")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)   # nothing happened inline
        # a rejected action must not leave an orphan job behind
        self.client("view@t").post(f"/api/items/{item['id']}/comments", json={"body": "x"})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_notifications_go_to_the_right_people_only(self):
        item = self.mk(who="alice@t")
        self.client("lead@t").post(f"/api/items/{item['id']}/assign", json={"assignee_id": self.uid["bob@t"], "expected_version": item["version"]})
        worker.drain(self.db)
        self.assertEqual(len(self.notifs("bob@t")), 1)         # new assignee
        self.assertEqual(len(self.notifs("alice@t")), 1)       # creator is a participant
        self.assertEqual(len(self.notifs("lead@t")), 0)        # never notify the actor
        self.assertEqual(len(self.notifs("erin@t")), 0)        # other team

    def test_failing_handler_retries_with_backoff_then_succeeds_exactly_once(self):
        calls = {"n": 0}
        real = worker.HANDLERS["notify_event"]

        def flaky(db, payload, now):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("smtp down")
            return real(db, payload, now)
        worker.HANDLERS["notify_event"] = flaky
        item = self.mk(who="alice@t")
        self.client("bob@t").post(f"/api/items/{item['id']}/comments", json={"body": "hi"})
        t0 = now_dt()
        worker.drain(self.db, t0)                              # attempt 1 fails for both queued jobs
        job = self.db.execute("SELECT * FROM jobs WHERE status='pending' AND attempts=1 ORDER BY id").fetchone()
        self.assertIn("smtp down", job["last_error"])
        self.assertGreater(job["run_at"], to_iso(t0))          # delayed, not hot-looping
        self.assertEqual(worker.drain(self.db, t0 + timedelta(seconds=1)), 0)   # not due yet
        worker.drain(self.db, t0 + timedelta(minutes=10))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE status!='done'").fetchone()[0], 0)
        self.assertEqual(len(self.notifs("alice@t")), 1)       # bob's comment -> alice, once

    def test_running_the_same_job_twice_creates_no_duplicates(self):
        item = self.mk(who="alice@t")
        self.client("bob@t").post(f"/api/items/{item['id']}/comments", json={"body": "hi"})
        worker.drain(self.db)
        n = self.db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        self.db.execute("UPDATE jobs SET status='pending', run_at=?", (to_iso(now_dt() - timedelta(seconds=1)),))   # redelivery
        worker.drain(self.db)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], n)

    def test_job_is_dead_lettered_after_max_attempts_and_does_not_block_others(self):
        def boom(db, payload, now):
            raise RuntimeError("permanent")
        worker.HANDLERS["notify_event"] = boom
        self.mk(who="alice@t")
        t = now_dt()
        for i in range(10):
            worker.drain(self.db, t + timedelta(hours=i))
        j = self.db.execute("SELECT * FROM jobs").fetchone()
        self.assertEqual((j["status"], j["attempts"]), ("dead", j["max_attempts"]))
        self.assertEqual(j["last_error"], "permanent")
        # the system keeps working: new actions still succeed and enqueue
        self.mk(who="bob@t")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)

    def test_crashed_worker_lease_expires_and_job_is_reclaimed(self):
        item = self.mk(who="alice@t")
        self.client("bob@t").post(f"/api/items/{item['id']}/comments", json={"body": "hi"})
        t = now_dt()
        claimed = worker.claim(self.db, t)                     # worker takes the job ... and dies
        self.assertEqual(claimed["status"], "running")
        second = worker.claim(self.db, t)                      # the other queued job
        self.assertNotEqual(second["id"], claimed["id"])      # a leased job is never handed to a second worker
        self.assertIsNone(worker.claim(self.db, t))           # everything is leased: nothing left to take
        worker.drain(self.db, t + timedelta(seconds=worker.LEASE_SECONDS + 5))   # lease expired -> picked up again
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE status!='done'").fetchone()[0], 0)

    def test_enqueue_dedupe_key_makes_scheduling_idempotent(self):
        for _ in range(3):
            worker.enqueue(self.db, "stale_sweep", {}, dedupe_key="stale_sweep:2026101813")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_stale_sweep_nags_owner_once_per_day(self):
        item = self.mk(who="alice@t")
        self.client("alice@t").post(f"/api/items/{item['id']}/claim", json={})
        old = to_iso(now_dt() - timedelta(days=10))
        self.db.execute("UPDATE work_items SET last_activity_at=? WHERE id=?", (old, item["id"]))
        worker.drain(self.db)                                  # flush event notifications first
        base = self.db.execute("SELECT COUNT(*) FROM notifications WHERE kind='stale'").fetchone()[0]
        for _ in range(2):
            worker.enqueue(self.db, "stale_sweep", {}, delay=0)
            worker.drain(self.db)
        stale = self.db.execute("SELECT * FROM notifications WHERE kind='stale'").fetchall()
        self.assertEqual((base, len(stale)), (0, 1))
        self.assertEqual(stale[0]["user_id"], self.uid["alice@t"])
