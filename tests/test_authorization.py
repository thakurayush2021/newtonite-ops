"""Critical behaviour: authorization is enforced by the API (not the UI) at team AND resource level."""
from .base import ApiTestCase


class TeamIsolation(ApiTestCase):
    def test_non_member_cannot_see_or_touch_items_of_other_teams(self):
        item = self.mk(who="elead@t", team="Eng", title="Secret engineering thing")
        outsider = self.client("bob@t")                       # Support only
        self.assertEqual(outsider.get(f"/api/items/{item['id']}").status_code, 404)   # 404, not 403: no existence leak
        self.assertEqual(outsider.get(f"/api/items/{item['id']}/events").status_code, 404)
        for path, body in [("claim", {}), ("comments", {"body": "hi"}), ("transition", {"to": "blocked", "expected_version": 1})]:
            self.assertEqual(outsider.post(f"/api/items/{item['id']}/{path}", json=body).status_code, 404, path)
        self.assertEqual(outsider.patch(f"/api/items/{item['id']}", json={"title": "x", "expected_version": 1}).status_code, 404)
        ids = [i["id"] for i in outsider.get("/api/items").get_json()["items"]]
        self.assertNotIn(item["id"], ids)
        self.assertNotIn("Secret", str(outsider.get("/api/items?q=secret").get_json()))

    def test_cannot_create_in_a_team_you_do_not_belong_to(self):
        r = self.client("bob@t").post("/api/items", json={"team_id": self.tid["Eng"], "title": "x"})
        self.assertEqual(r.status_code, 403)

    def test_roles_are_per_team(self):
        # alice is a member in both; lead@t is lead only in Support
        eng = self.mk(who="elead@t", team="Eng")
        r = self.client("lead@t").post(f"/api/items/{eng['id']}/claim", json={})
        self.assertEqual(r.status_code, 404)                  # lead in Support has no power in Eng

    def test_changes_feed_and_dashboard_are_scoped(self):
        c = self.client("bob@t")
        head = c.get("/api/changes").get_json()["cursor"]
        self.mk(who="elead@t", team="Eng")
        feed = c.get(f"/api/changes?since={head}").get_json()
        self.assertEqual(feed["item_ids"], [])
        self.assertEqual(c.get("/api/dashboard").get_json()["counts"]["active"], 0)


class ResourceLevel(ApiTestCase):
    def test_viewer_is_read_only(self):
        item = self.mk()
        v = self.client("view@t")
        self.assertEqual(v.get(f"/api/items/{item['id']}").status_code, 200)
        self.assertEqual(v.post(f"/api/items/{item['id']}/claim", json={}).status_code, 403)
        self.assertEqual(v.post(f"/api/items/{item['id']}/comments", json={"body": "x"}).status_code, 403)
        self.assertEqual(v.post("/api/items", json={"team_id": self.tid["Support"], "title": "x"}).status_code, 403)
        self.assertFalse(self.get("view@t", item["id"])["can"]["comment"])

    def test_member_cannot_change_priority_or_assign_others(self):
        item = self.mk(who="alice@t")
        a = self.client("alice@t")
        r = a.patch(f"/api/items/{item['id']}", json={"priority": "urgent", "expected_version": item["version"]})
        self.assertEqual(r.status_code, 403)
        r = a.post(f"/api/items/{item['id']}/assign", json={"assignee_id": self.uid["bob@t"], "expected_version": item["version"]})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.get("alice@t", item["id"])["priority"], "normal")

    def test_lead_can_change_priority_and_it_is_recorded(self):
        item = self.mk(who="alice@t")
        r = self.client("lead@t").patch(f"/api/items/{item['id']}", json={"priority": "urgent", "expected_version": item["version"]})
        self.assertEqual(r.status_code, 200)
        self.assertIn("priority_changed", self.event_types(item["id"]))

    def test_member_cannot_edit_or_move_someone_elses_item(self):
        item = self.mk(who="alice@t")
        self.client("alice@t").post(f"/api/items/{item['id']}/claim", json={})
        item = self.get("alice@t", item["id"])
        bob = self.client("bob@t")                            # same team, neither creator nor assignee
        self.assertEqual(bob.patch(f"/api/items/{item['id']}", json={"title": "hijack", "expected_version": item["version"]}).status_code, 403)
        self.assertEqual(bob.post(f"/api/items/{item['id']}/transition", json={"to": "blocked", "expected_version": item["version"]}).status_code, 403)
        self.assertEqual(bob.post(f"/api/items/{item['id']}/release", json={"expected_version": item["version"]}).status_code, 403)

    def test_only_lead_can_assign_and_target_must_be_a_working_member(self):
        item = self.mk()
        lead = self.client("lead@t")
        ok = lead.post(f"/api/items/{item['id']}/assign", json={"assignee_id": self.uid["bob@t"], "expected_version": item["version"]})
        self.assertEqual(ok.status_code, 200)
        v = ok.get_json()["item"]["version"]
        for who in ("view@t", "erin@t"):                      # a viewer, and someone outside the team
            r = lead.post(f"/api/items/{item['id']}/assign", json={"assignee_id": self.uid[who], "expected_version": v})
            self.assertEqual(r.status_code, 409, who)

    def test_membership_management_requires_team_admin(self):
        body = {"email": "erin@t", "role": "member"}
        self.assertEqual(self.client("lead@t").put(f"/api/teams/{self.tid['Support']}/members", json=body).status_code, 403)
        self.assertEqual(self.client("tadmin@t").put(f"/api/teams/{self.tid['Support']}/members", json=body).status_code, 200)
        self.assertEqual(self.client("root@t").delete(f"/api/teams/{self.tid['Support']}/members/{self.uid['erin@t']}").status_code, 200)

    def test_unauthenticated_requests_are_rejected(self):
        c = self.app.test_client()
        self.assertEqual(c.get("/api/items").status_code, 401)
        self.assertEqual(c.get("/api/items", headers={"Authorization": "Bearer nope"}).status_code, 401)

    def test_removed_member_loses_access_and_their_active_work_is_released(self):
        item = self.mk(who="alice@t")
        self.client("bob@t").post(f"/api/items/{item['id']}/claim", json={})
        self.client("tadmin@t").delete(f"/api/teams/{self.tid['Support']}/members/{self.uid['bob@t']}")
        self.assertEqual(self.client("bob@t").get(f"/api/items/{item['id']}").status_code, 404)
        self.assertIsNone(self.get("alice@t", item["id"])["assignee_id"])
