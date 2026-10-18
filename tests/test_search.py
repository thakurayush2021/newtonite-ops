"""Critical behaviour: listing stays correct and scoped as data grows (keyset pagination, FTS, filters)."""
from .base import ApiTestCase


class Listing(ApiTestCase):
    def seed_many(self, n, team="Support"):
        c = self.client("alice@t")
        ids = []
        for i in range(n):
            r = c.post("/api/items", json={"team_id": self.tid[team], "title": f"Ticket number {i}",
                                           "priority": ["low", "normal", "high", "urgent"][i % 4]})
            ids.append(r.get_json()["item"]["id"])
        return ids

    def test_cursor_pagination_returns_every_item_exactly_once_even_while_new_items_arrive(self):
        ids = self.seed_many(35)
        c = self.client("alice@t")
        seen, cursor, page = [], None, 0
        while True:
            r = c.get("/api/items?limit=10" + (f"&cursor={cursor}" if cursor else "")).get_json()
            seen += [i["id"] for i in r["items"]]
            page += 1
            if page == 1:
                self.seed_many(5)                    # new items land mid-scroll: offset paging would now duplicate rows
            cursor = r["next_cursor"]
            if not cursor:
                break
        self.assertEqual(len(seen), len(set(seen)), "no duplicates")
        self.assertTrue(set(ids) <= set(seen), "nothing skipped")

    def test_priority_sort_is_stable_across_pages(self):
        self.seed_many(24)
        c = self.client("alice@t")
        p1 = c.get("/api/items?sort=priority&limit=10").get_json()
        p2 = c.get(f"/api/items?sort=priority&limit=10&cursor={p1['next_cursor']}").get_json()
        ranks = [{"low": 0, "normal": 1, "high": 2, "urgent": 3}[i["priority"]] for i in p1["items"] + p2["items"]]
        self.assertEqual(ranks, sorted(ranks, reverse=True))
        self.assertEqual(p1["items"][0]["priority"], "urgent")

    def test_full_text_search_filters_and_updates_with_edits(self):
        a = self.client("alice@t")
        x = self.mk(title="Payout stuck for merchant", description="settlement file mismatch")
        self.mk(title="Printer is out of toner")
        titles = lambda q: [i["title"] for i in a.get("/api/items", query_string=q).get_json()["items"]]
        self.assertEqual(titles({"q": "payo"}), ["Payout stuck for merchant"])        # prefix
        self.assertEqual(titles({"q": "settlement"}), ["Payout stuck for merchant"])  # description
        self.assertEqual(titles({"q": f"#{x['id']}"}), ["Payout stuck for merchant"]) # by id
        self.assertEqual(titles({"q": 'toner" OR "'}), [])                            # no FTS syntax injection / no 500
        a.patch(f"/api/items/{x['id']}", json={"title": "Chargeback evidence", "expected_version": x["version"]})
        self.assertEqual(titles({"q": "payout"}), [])                                  # index follows the edit
        self.assertEqual(titles({"q": "chargeback"}), ["Chargeback evidence"])

    def test_filters_and_named_views(self):
        a, lead = self.client("alice@t"), self.client("lead@t")
        u = self.mk(title="urgent unowned", priority="urgent")
        mine = self.mk(title="mine")
        a.post(f"/api/items/{mine['id']}/claim", json={})
        names = lambda q: sorted(i["title"] for i in a.get("/api/items", query_string=q).get_json()["items"])
        self.assertEqual(names({"view": "mine"}), ["mine"])
        self.assertEqual(names({"view": "unassigned"}), ["urgent unowned"])
        self.assertEqual(names({"view": "attention"}), ["urgent unowned"])        # urgent+nobody owns it
        self.assertEqual(names({"priority": "urgent"}), ["urgent unowned"])
        self.assertEqual(names({"assignee": "none", "status": "open"}), ["urgent unowned"])
        self.assertEqual(a.get("/api/items?status=bogus").status_code, 400)
        self.assertEqual(a.get("/api/items?cursor=!!!").status_code, 400)
        c = lead.get("/api/dashboard").get_json()["counts"]
        self.assertEqual((c["mine"], c["unassigned"], c["attention"]), (0, 1, 1))

    def test_approvals_queue_only_shows_items_a_lead_can_actually_review(self):
        item = self.mk(who="alice@t", requires_approval=True)
        a = self.client("alice@t")
        a.post(f"/api/items/{item['id']}/claim", json={})
        v = self.get("alice@t", item["id"])["version"]
        a.post(f"/api/items/{item['id']}/transition", json={"to": "in_progress", "expected_version": v})
        a.post(f"/api/items/{item['id']}/transition", json={"to": "awaiting_approval", "resolution": "ok", "expected_version": v + 1})
        q = lambda who: len(self.client(who).get("/api/items?view=approvals").get_json()["items"])
        self.assertEqual((q("lead@t"), q("alice@t"), q("bob@t"), q("elead@t")), (1, 0, 0, 0))

    def test_change_feed_reports_only_changed_visible_items(self):
        c = self.client("alice@t")
        head = c.get("/api/changes").get_json()["cursor"]
        x, y = self.mk(title="x"), self.mk(title="y")
        self.client("bob@t").post(f"/api/items/{x['id']}/comments", json={"body": "hey"})
        feed = c.get(f"/api/changes?since={head}").get_json()
        self.assertEqual(sorted(feed["item_ids"]), sorted([x["id"], y["id"]]))
        again = c.get(f"/api/changes?since={feed['cursor']}").get_json()
        self.assertEqual(again["item_ids"], [])

    def test_oversized_and_malformed_input_is_rejected_cleanly(self):
        a = self.client("alice@t")
        self.assertEqual(a.post("/api/items", json={"team_id": self.tid["Support"], "title": "x" * 500}).status_code, 400)
        self.assertEqual(a.post("/api/items", data="not json", content_type="application/json").status_code, 400)
        self.assertEqual(a.post("/api/items", json={"team_id": self.tid["Support"], "title": "ok", "due_at": "tomorrow-ish"}).status_code, 400)
        self.assertEqual(a.post("/api/items", json={"team_id": self.tid["Support"], "title": "ok", "priority": "meh"}).status_code, 400)
