"""Critical behaviour: workflow rules cannot be bypassed; history cannot silently disappear."""
import sqlite3

from .base import ApiTestCase


class Workflow(ApiTestCase):
    def tr(self, who, item, to, **kw):
        it = self.get(who, item["id"])
        return self.client(who).post(f"/api/items/{item['id']}/transition",
                                     json={"to": to, "expected_version": it["version"], **kw})

    def test_must_be_owned_before_in_progress(self):
        item = self.mk()
        r = self.tr("lead@t", item, "in_progress")
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (409, "assignee_required"))

    def test_illegal_transition_is_rejected_with_allowed_list(self):
        item = self.mk()
        r = self.tr("lead@t", item, "resolved")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"]["code"], "invalid_transition")

    def test_resolving_requires_a_resolution_note(self):
        item = self.mk()
        self.client("alice@t").post(f"/api/items/{item['id']}/claim", json={})
        self.assertEqual(self.tr("alice@t", item, "in_progress").status_code, 200)
        self.assertEqual(self.tr("alice@t", item, "resolved").status_code, 400)
        r = self.tr("alice@t", item, "resolved", resolution="Fixed the config")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["item"]["resolution"], "Fixed the config")

    def _awaiting(self):
        item = self.mk(who="alice@t", requires_approval=True)
        self.client("alice@t").post(f"/api/items/{item['id']}/claim", json={})
        self.assertEqual(self.tr("alice@t", item, "in_progress").status_code, 200)
        return item

    def test_approval_cannot_be_skipped(self):
        item = self._awaiting()
        r = self.tr("alice@t", item, "resolved", resolution="done")
        self.assertEqual(r.status_code, 409)
        self.assertIn("requires approval", r.get_json()["error"]["message"])
        self.assertEqual(self.tr("lead@t", item, "resolved", resolution="done").status_code, 409)   # even for a lead

    def test_requester_cannot_approve_own_request_but_another_lead_can(self):
        item = self._awaiting()
        self.assertEqual(self.tr("alice@t", item, "awaiting_approval", resolution="Please approve").status_code, 200)
        cur = self.get("alice@t", item["id"])
        self.assertEqual(cur["status"], "awaiting_approval")
        # the requester (alice, member) cannot approve: wrong role
        r = self.client("alice@t").post(f"/api/items/{item['id']}/approve", json={"expected_version": cur["version"]})
        self.assertEqual(r.status_code, 403)
        # a lead who is ALSO the requester must be blocked by separation of duties
        self.db.execute("UPDATE memberships SET role='lead' WHERE user_id=? AND team_id=?", (self.uid["alice@t"], self.tid["Support"]))
        r = self.client("alice@t").post(f"/api/items/{item['id']}/approve", json={"expected_version": cur["version"]})
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (403, "separation_of_duties"))
        self.db.execute("UPDATE memberships SET role='member' WHERE user_id=? AND team_id=?", (self.uid["alice@t"], self.tid["Support"]))
        # an independent lead can
        r = self.client("lead@t").post(f"/api/items/{item['id']}/approve", json={"expected_version": cur["version"], "note": "LGTM"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["item"]["status"], "resolved")

    def test_two_leads_reviewing_at_once_only_one_decision_is_recorded(self):
        item = self._awaiting()
        self.tr("alice@t", item, "awaiting_approval", resolution="r")
        v = self.get("alice@t", item["id"])["version"]
        a, b = self.client("lead@t"), self.client("tadmin@t")
        r1 = a.post(f"/api/items/{item['id']}/approve", json={"expected_version": v})
        r2 = b.post(f"/api/items/{item['id']}/reject", json={"expected_version": v, "note": "no"})
        self.assertEqual(sorted([r1.status_code, r2.status_code]), [200, 409])
        decisions = [t for t in self.event_types(item["id"]) if t in ("approved", "rejected")]
        self.assertEqual(len(decisions), 1)

    def test_rejection_requires_a_reason_and_returns_item_to_work(self):
        item = self._awaiting()
        self.tr("alice@t", item, "awaiting_approval", resolution="r")
        v = self.get("alice@t", item["id"])["version"]
        self.assertEqual(self.client("lead@t").post(f"/api/items/{item['id']}/reject", json={"expected_version": v}).status_code, 400)
        r = self.client("lead@t").post(f"/api/items/{item['id']}/reject", json={"expected_version": v, "note": "Add a test"})
        self.assertEqual(r.get_json()["item"]["status"], "in_progress")

    def test_releasing_started_work_puts_it_back_in_the_queue(self):
        item = self.mk()
        a = self.client("alice@t")
        a.post(f"/api/items/{item['id']}/claim", json={})
        self.tr("alice@t", item, "in_progress")
        cur = self.get("alice@t", item["id"])
        r = a.post(f"/api/items/{item['id']}/release", json={"expected_version": cur["version"]})
        self.assertEqual((r.get_json()["item"]["status"], r.get_json()["item"]["assignee_id"]), ("open", None))

    def test_database_itself_rejects_invariant_violations(self):
        item = self.mk()
        with self.assertRaises(sqlite3.IntegrityError):       # in_progress without owner, bypassing the app entirely
            self.db.execute("UPDATE work_items SET status='in_progress' WHERE id=?", (item["id"],))

    def test_only_leads_close_and_reopen(self):
        item = self.mk(who="alice@t")
        self.assertEqual(self.tr("alice@t", item, "closed").status_code, 403)
        self.assertEqual(self.tr("lead@t", item, "closed").status_code, 200)
        self.assertEqual(self.tr("alice@t", item, "open").status_code, 403)
        self.assertEqual(self.tr("lead@t", item, "open").status_code, 200)
        self.assertEqual(self.client("alice@t").patch(f"/api/items/{item['id']}", json={"title": "x", "expected_version": 99}).status_code, 409)

    def test_closed_items_are_read_only(self):
        item = self.mk(who="alice@t")
        self.tr("lead@t", item, "closed")
        cur = self.get("alice@t", item["id"])
        r = self.client("lead@t").patch(f"/api/items/{item['id']}", json={"title": "x", "expected_version": cur["version"]})
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (409, "item_closed"))
        self.assertEqual(self.client("alice@t").post(f"/api/items/{item['id']}/claim", json={}).status_code, 409)


class History(ApiTestCase):
    def test_every_important_action_is_recorded_in_order(self):
        item = self.mk(who="alice@t")
        a, lead = self.client("alice@t"), self.client("lead@t")
        a.post(f"/api/items/{item['id']}/claim", json={})
        v = self.get("alice@t", item["id"])["version"]
        lead.patch(f"/api/items/{item['id']}", json={"priority": "high", "expected_version": v})
        v += 1
        a.post(f"/api/items/{item['id']}/transition", json={"to": "in_progress", "expected_version": v})
        a.post(f"/api/items/{item['id']}/comments", json={"body": "Working on it"})
        self.assertEqual(self.event_types(item["id"]),
                         ["created", "assigned", "priority_changed", "status_changed", "comment"])
        evs = a.get(f"/api/items/{item['id']}/events").get_json()["events"]
        self.assertEqual(evs[0]["type"], "comment")            # newest first
        pri = next(e for e in evs if e["type"] == "priority_changed")
        self.assertEqual((pri["data"]["from"], pri["data"]["to"], pri["actor_name"]), ("normal", "high", "Lead"))

    def test_history_cannot_be_edited_or_deleted_even_with_database_access(self):
        item = self.mk()
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.execute("DELETE FROM events WHERE item_id=?", (item["id"],))
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.execute("UPDATE events SET type='nothing' WHERE item_id=?", (item["id"],))

    def test_failed_actions_leave_no_trace(self):
        item = self.mk()
        before = self.event_types(item["id"])
        self.client("view@t").post(f"/api/items/{item['id']}/claim", json={})
        self.client("lead@t").post(f"/api/items/{item['id']}/transition", json={"to": "resolved", "expected_version": 1})
        self.assertEqual(self.event_types(item["id"]), before)
