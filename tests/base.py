import os
import tempfile
import unittest

from app import create_app
from app import db as dbmod
from app.common import now_iso
from werkzeug.security import generate_password_hash

PW = "pw-test-1234"

# team -> list of (email, role)
ROSTER = {
    "Support": [("lead@t", "lead"), ("alice@t", "member"), ("bob@t", "member"), ("view@t", "viewer"), ("tadmin@t", "admin")],
    "Eng": [("elead@t", "lead"), ("alice@t", "member"), ("erin@t", "member")],
}


class ApiTestCase(unittest.TestCase):
    """Fresh database + app per test. Background worker disabled so tests drive jobs deterministically."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.path)
        self.app = create_app({"DATABASE": self.path, "WORKER_ENABLED": False, "HASH_METHOD": "pbkdf2:sha256:1000"})
        self.db = dbmod.connect(self.path)
        ph = generate_password_hash(PW, method="pbkdf2:sha256:1000")
        self.uid, self.tid = {}, {}
        emails = {e for t in ROSTER.values() for e, _ in t} | {"root@t"}
        for e in sorted(emails):
            self.uid[e] = self.db.execute(
                "INSERT INTO users(email,name,password_hash,is_admin,created_at) VALUES (?,?,?,?,?)",
                (e, e.split("@")[0].title(), ph, int(e == "root@t"), now_iso())).lastrowid
        for t, members in ROSTER.items():
            self.tid[t] = self.db.execute("INSERT INTO teams(name) VALUES (?)", (t,)).lastrowid
            for e, r in members:
                self.db.execute("INSERT INTO memberships VALUES (?,?,?)", (self.tid[t], self.uid[e], r))
        self._clients = {}

    def tearDown(self):
        self.db.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + ext)
            except FileNotFoundError:
                pass

    # -- helpers --
    def client(self, email):
        """A logged-in client; each call to this returns an independent client (own session)."""
        c = self.app.test_client()
        r = c.post("/api/auth/login", json={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.get_json())
        c.environ_base["HTTP_AUTHORIZATION"] = "Bearer " + r.get_json()["token"]
        return c

    def mk(self, who="alice@t", team="Support", **kw):
        body = {"team_id": self.tid[team], "title": kw.pop("title", "Something broke"), **kw}
        r = self.client(who).post("/api/items", json=body)
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()["item"]

    def get(self, who, item_id):
        return self.client(who).get(f"/api/items/{item_id}").get_json()["item"]

    def event_types(self, item_id):
        return [r["type"] for r in self.db.execute("SELECT type FROM events WHERE item_id=? ORDER BY id", (item_id,))]
