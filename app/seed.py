"""Demo data. `python -m app.seed --items 20000` to see the app at scale."""
import argparse
import json
import os
import random
from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

from . import db as dbmod
from .common import to_iso
from .workflow import PRANK

PASSWORD = "password123"
USERS = [  # email, name, is_admin
    ("ada@newtonite.test", "Ada Admin", 1), ("priya@newtonite.test", "Priya Lead", 0),
    ("sam@newtonite.test", "Sam Support", 0), ("maya@newtonite.test", "Maya Multi", 0),
    ("vik@newtonite.test", "Vik Viewer", 0), ("ethan@newtonite.test", "Ethan Lead", 0),
    ("nora@newtonite.test", "Nora Eng", 0), ("omar@newtonite.test", "Omar Eng", 0),
    ("paula@newtonite.test", "Paula Payments", 0), ("quinn@newtonite.test", "Quinn Pay", 0),
    ("carla@newtonite.test", "Carla Compliance", 0), ("dev@newtonite.test", "Dev Compliance", 0),
]
TEAMS = ["Support", "Engineering", "Payments", "Compliance"]
MEMBERS = {  # team -> [(email, role)]
    "Support": [("ada@newtonite.test", "admin"), ("priya@newtonite.test", "lead"), ("sam@newtonite.test", "member"),
                ("maya@newtonite.test", "member"), ("nora@newtonite.test", "member"), ("vik@newtonite.test", "viewer")],
    "Engineering": [("ada@newtonite.test", "admin"), ("ethan@newtonite.test", "lead"), ("nora@newtonite.test", "member"),
                    ("omar@newtonite.test", "member"), ("priya@newtonite.test", "member")],
    "Payments": [("ada@newtonite.test", "admin"), ("paula@newtonite.test", "lead"), ("quinn@newtonite.test", "member"),
                 ("maya@newtonite.test", "member"), ("carla@newtonite.test", "member")],
    "Compliance": [("ada@newtonite.test", "admin"), ("carla@newtonite.test", "lead"), ("dev@newtonite.test", "member"),
                   ("paula@newtonite.test", "viewer")],
}
TOPICS = {
    "Support": ["Customer cannot log in after password reset", "Refund requested for duplicate charge", "Enterprise customer reports slow dashboards",
                "Invoice PDF missing company logo", "Customer asks for data export", "Account locked after failed MFA", "Wrong currency shown on receipt"],
    "Engineering": ["API latency spike on /orders", "Production incident: queue backlog growing", "Memory leak in export worker",
                    "Migrate cron jobs to scheduler", "Flaky integration test in checkout", "Rotate expiring TLS certificates", "Cache stampede on pricing endpoint"],
    "Payments": ["Payout stuck in pending for 48h", "Chargeback evidence due", "Reconcile settlement file mismatch",
                 "Card network declined with code 05 spike", "Investigate suspected duplicate payment", "FX rate rounding discrepancy"],
    "Compliance": ["Subject access request from EU customer", "Quarterly access review sign-off", "Vendor security questionnaire",
                   "Retention policy exception request", "AML alert requires analyst review", "Audit evidence collection"],
}


def seed(path, n_items=400, hash_method="pbkdf2:sha256:100000"):
    dbmod.init_db(path)
    c = dbmod.connect(path)
    if c.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        c.close()
        return
    rnd = random.Random(7)
    now = datetime.now(timezone.utc)
    c.execute("BEGIN")
    ph = generate_password_hash(PASSWORD, method=hash_method)
    uid = {}
    for email, name, adm in USERS:
        uid[email] = c.execute("INSERT INTO users(email,name,password_hash,is_admin,created_at) VALUES (?,?,?,?,?)",
                               (email, name, ph, adm, to_iso(now))).lastrowid
    tid = {}
    for t in TEAMS:
        tid[t] = c.execute("INSERT INTO teams(name) VALUES (?)", (t,)).lastrowid
        for email, role in MEMBERS[t]:
            c.execute("INSERT INTO memberships VALUES (?,?,?)", (tid[t], uid[email], role))

    for i in range(n_items):
        team = rnd.choice(TEAMS)
        members = [(e, r) for e, r in MEMBERS[team] if r in ("member", "lead", "admin")]
        creator = uid[rnd.choice(members)[0]]
        created = now - timedelta(days=rnd.random() * 60, minutes=rnd.randint(0, 600))
        status = rnd.choices(["open", "in_progress", "blocked", "awaiting_approval", "resolved", "closed"], [28, 20, 7, 5, 20, 20])[0]
        prio = rnd.choices(["low", "normal", "high", "urgent"], [20, 50, 22, 8])[0]
        assignee = None
        if status != "open" or rnd.random() < 0.4:
            assignee = uid[rnd.choice(members)[0]]
        if status in ("in_progress", "awaiting_approval") and assignee is None:
            assignee = creator
        req = 1 if (status == "awaiting_approval" or rnd.random() < 0.15) else 0
        appr_state, appr_by, resolution = "none", None, None
        if status == "awaiting_approval":
            appr_state, appr_by, resolution = "pending", assignee, "Fix verified in staging; ready to resolve."
        elif status in ("resolved", "closed"):
            resolution = "Resolved with customer; confirmed no recurrence."
            if req:
                appr_state, appr_by = "approved", assignee
        due = None
        if rnd.random() < 0.55:
            due = to_iso(now + timedelta(days=rnd.randint(-6, 14), hours=rnd.randint(0, 20)))
        updated = created + timedelta(hours=rnd.random() * 24 * min(50, max(1, (now - created).days)))
        updated = min(updated, now)
        if status in ("open", "in_progress", "blocked", "awaiting_approval") and rnd.random() < 0.75:
            updated = now - timedelta(hours=rnd.random() * 24 * 3)      # most live work is recently touched
        title = f"{rnd.choice(TOPICS[team])} ({rnd.randint(1000, 9999)})"
        desc = ("Reported via chat and email. Needs investigation and a clear owner. "
                f"Reference: ORD-{rnd.randint(10000, 99999)}. Impacted customers: {rnd.randint(1, 400)}.")
        iid = c.execute(
            "INSERT INTO work_items(team_id,title,description,status,priority,prank,assignee_id,creator_id,due_at,requires_approval,"
            "approval_state,approval_requested_by,resolution,version,created_at,updated_at,last_activity_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid[team], title, desc, status, prio, PRANK[prio], assignee, creator, due, req, appr_state, appr_by, resolution,
             1, to_iso(created), to_iso(updated), to_iso(updated))).lastrowid
        c.execute("INSERT INTO events(item_id,actor_id,type,data,created_at) VALUES (?,?,?,?,?)",
                  (iid, creator, "created", json.dumps({"title": title, "priority": prio}), to_iso(created)))
        if assignee:
            c.execute("INSERT INTO events(item_id,actor_id,type,data,created_at) VALUES (?,?,?,?,?)",
                      (iid, assignee, "assigned", json.dumps({"from": None, "to": assignee, "via": "claim"}), to_iso(created + timedelta(minutes=5))))
        if status != "open":
            c.execute("INSERT INTO events(item_id,actor_id,type,data,created_at) VALUES (?,?,?,?,?)",
                      (iid, assignee or creator, "status_changed", json.dumps({"from": "open", "to": status}), to_iso(updated)))
    c.execute("COMMIT")
    c.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("DATABASE", "newtonite.db"))
    ap.add_argument("--items", type=int, default=400)
    ap.add_argument("--reset", action="store_true", help="delete the database first")
    a = ap.parse_args()
    if a.reset:
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(a.db + ext):
                os.remove(a.db + ext)
    seed(a.db, a.items)
    print(f"Seeded {a.db} with {a.items} items. Log in as any *@newtonite.test user, password: {PASSWORD}")
