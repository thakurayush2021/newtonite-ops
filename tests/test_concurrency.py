"""Critical behaviour #1-#3: simultaneous claims, duplicate submissions, stale writes."""
import threading
import uuid

from .base import ApiTestCase


def run_parallel(fns):
    barrier, out = threading.Barrier(len(fns)), [None] * len(fns)

    def wrap(i, fn):
        barrier.wait()
        out[i] = fn()
    ts = [threading.Thread(target=wrap, args=(i, f)) for i, f in enumerate(fns)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


class ClaimRace(ApiTestCase):
    def test_exactly_one_of_many_simultaneous_claims_wins(self):
        item = self.mk(who="lead@t")
        users = ["alice@t", "bob@t", "lead@t", "tadmin@t"]
        clients = [self.client(u) for u in users]
        res = run_parallel([lambda c=c: c.post(f"/api/items/{item['id']}/claim", json={}) for c in clients])
        codes = sorted(r.status_code for r in res)
        self.assertEqual(codes, [200, 409, 409, 409])
        loser = next(r for r in res if r.status_code == 409).get_json()["error"]
        self.assertEqual(loser["code"], "already_claimed")
        self.assertIn("current", loser["details"])           # loser can reconcile its UI immediately
        winner_id = self.get("lead@t", item["id"])["assignee_id"]
        assigned_events = self.db.execute("SELECT COUNT(*) FROM events WHERE item_id=? AND type='assigned'", (item["id"],)).fetchone()[0]
        self.assertEqual(assigned_events, 1)                  # history records exactly one winner
        self.assertIn(winner_id, [self.uid[u] for u in users])

    def test_repeating_your_own_claim_is_a_harmless_noop(self):
        item = self.mk()
        c = self.client("alice@t")
        self.assertEqual(c.post(f"/api/items/{item['id']}/claim", json={}).status_code, 200)
        r = c.post(f"/api/items/{item['id']}/claim", json={})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["already"])
        self.assertEqual(self.event_types(item["id"]).count("assigned"), 1)


class Idempotency(ApiTestCase):
    def test_retrying_create_with_same_key_creates_one_item(self):
        c, key = self.client("alice@t"), str(uuid.uuid4())
        body = {"team_id": self.tid["Support"], "title": "Double click"}
        r1 = c.post("/api/items", json=body, headers={"Idempotency-Key": key})
        r2 = c.post("/api/items", json=body, headers={"Idempotency-Key": key})
        self.assertEqual((r1.status_code, r2.status_code), (201, 201))
        self.assertEqual(r1.get_json()["item"]["id"], r2.get_json()["item"]["id"])
        self.assertEqual(r2.headers.get("Idempotent-Replayed"), "true")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM work_items").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_simultaneous_duplicates_with_same_key_execute_once(self):
        key, body = str(uuid.uuid4()), {"team_id": self.tid["Support"], "title": "Race"}
        clients = [self.client("alice@t") for _ in range(6)]
        res = run_parallel([lambda c=c: c.post("/api/items", json=body, headers={"Idempotency-Key": key}) for c in clients])
        self.assertTrue(all(r.status_code == 201 for r in res))
        self.assertEqual(len({r.get_json()["item"]["id"] for r in res}), 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM work_items").fetchone()[0], 1)

    def test_same_key_different_payload_is_rejected(self):
        c, key = self.client("alice@t"), "k-1"
        c.post("/api/items", json={"team_id": self.tid["Support"], "title": "A"}, headers={"Idempotency-Key": key})
        r = c.post("/api/items", json={"team_id": self.tid["Support"], "title": "B"}, headers={"Idempotency-Key": key})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM work_items").fetchone()[0], 1)

    def test_failed_requests_are_not_cached_so_retry_can_succeed(self):
        item = self.mk()
        lead, key = self.client("lead@t"), "assign-1"
        bad = lead.post(f"/api/items/{item['id']}/assign", json={"assignee_id": self.uid["alice@t"], "expected_version": 99},
                        headers={"Idempotency-Key": key})
        self.assertEqual(bad.status_code, 409)
        # same key, same body would replay a failure if we had cached it; the key must stay usable
        self.assertIsNone(self.db.execute("SELECT 1 FROM idempotency_keys WHERE key=?", (key,)).fetchone())

    def test_comment_double_submit_posts_once(self):
        item = self.mk()
        c, key = self.client("alice@t"), "c-1"
        for _ in range(3):
            c.post(f"/api/items/{item['id']}/comments", json={"body": "hello"}, headers={"Idempotency-Key": key})
        self.assertEqual(self.event_types(item["id"]).count("comment"), 1)


class StaleWrites(ApiTestCase):
    def test_stale_edit_is_rejected_with_current_state_and_nothing_is_overwritten(self):
        item = self.mk(who="alice@t", title="Original")
        a, lead = self.client("alice@t"), self.client("lead@t")
        v = item["version"]
        r = lead.patch(f"/api/items/{item['id']}", json={"title": "Lead's title", "expected_version": v})
        self.assertEqual(r.status_code, 200)
        r = a.patch(f"/api/items/{item['id']}", json={"title": "Alice's stale title", "expected_version": v})
        self.assertEqual(r.status_code, 409)
        err = r.get_json()["error"]
        self.assertEqual(err["code"], "version_conflict")
        self.assertEqual(err["details"]["current"]["title"], "Lead's title")
        self.assertEqual(err["details"]["last_change"]["actor_name"], "Lead")
        self.assertEqual(self.get("alice@t", item["id"])["title"], "Lead's title")

    def test_concurrent_edits_same_version_only_one_applies(self):
        item = self.mk(who="alice@t")
        c1, c2 = self.client("alice@t"), self.client("lead@t")
        v = item["version"]
        res = run_parallel([
            lambda: c1.patch(f"/api/items/{item['id']}", json={"title": "from alice", "expected_version": v}),
            lambda: c2.patch(f"/api/items/{item['id']}", json={"title": "from lead", "expected_version": v}),
        ])
        self.assertEqual(sorted(r.status_code for r in res), [200, 409])
        self.assertEqual(self.get("lead@t", item["id"])["version"], v + 1)

    def test_version_is_required_for_mutations(self):
        item = self.mk()
        r = self.client("alice@t").patch(f"/api/items/{item['id']}", json={"title": "x"})
        self.assertEqual(r.status_code, 400)

    def test_comments_do_not_invalidate_someone_elses_edit(self):
        item = self.mk(who="alice@t")
        self.client("bob@t").post(f"/api/items/{item['id']}/comments", json={"body": "fyi"})
        r = self.client("alice@t").patch(f"/api/items/{item['id']}", json={"title": "still fine", "expected_version": item["version"]})
        self.assertEqual(r.status_code, 200)
