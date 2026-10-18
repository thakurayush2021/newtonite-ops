"""Read-side queries. Every query is scoped to the caller's team memberships *in SQL* (JOIN memberships),
so a bug in a filter can never widen visibility."""
import base64
import json
import re
from datetime import timedelta

from . import authz, workflow
from .common import ApiError, now_dt, to_iso, validation

STALE_AFTER_DAYS = 3

_BASE_FROM = """
FROM work_items w
JOIN memberships m ON m.team_id = w.team_id AND m.user_id = :me
JOIN teams t ON t.id = w.team_id
JOIN users c ON c.id = w.creator_id
LEFT JOIN users a ON a.id = w.assignee_id
"""
_COLS = """w.*, t.name AS team_name, c.name AS creator_name, a.name AS assignee_name, m.role AS my_role"""

# --- named views ("queues"). Fragments use :me, :now, :stale. ---------------------------------
_ACT = "w.status IN ('open','in_progress','blocked','awaiting_approval')"
VIEWS = {
    "mine": f"(w.assignee_id = :me AND {_ACT})",
    "unassigned": "(w.assignee_id IS NULL AND w.status IN ('open','blocked'))",
    "overdue": f"(w.due_at IS NOT NULL AND w.due_at < :now AND {_ACT})",
    "approvals": ("(w.status = 'awaiting_approval' AND w.approval_requested_by != :me AND m.role IN ('lead','admin'))"),
    "stale": "(w.last_activity_at < :stale AND w.status IN ('open','in_progress','blocked'))",
    "active": f"({_ACT})",
    "all": "(1=1)",
}
VIEWS["attention"] = (
    f"({VIEWS['mine']} AND (w.prank >= 2 OR w.status = 'blocked' OR {VIEWS['overdue']} OR {VIEWS['stale']}))"
    f" OR {VIEWS['approvals']}"
    f" OR (w.assignee_id IS NULL AND w.prank >= 2 AND w.status IN ('open','blocked'))"
)

SORTS = {  # all descending so a single row-value comparison implements keyset pagination
    "updated": ("w.updated_at", "w.id"),
    "priority": ("w.prank", "w.updated_at", "w.id"),
    "created": ("w.created_at", "w.id"),
}


def _params(me):
    now = now_dt()
    return {"me": me, "now": to_iso(now), "stale": to_iso(now - timedelta(days=STALE_AFTER_DAYS))}


# --- serialization ---------------------------------------------------------------------------
def serialize_item(row, uid, full=False):
    d = {
        "id": row["id"], "team_id": row["team_id"], "team_name": row["team_name"],
        "title": row["title"], "status": row["status"], "priority": row["priority"],
        "assignee_id": row["assignee_id"], "assignee_name": row["assignee_name"],
        "creator_id": row["creator_id"], "creator_name": row["creator_name"],
        "due_at": row["due_at"], "requires_approval": bool(row["requires_approval"]),
        "approval_state": row["approval_state"], "approval_requested_by": row["approval_requested_by"],
        "resolution": row["resolution"], "version": row["version"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "last_activity_at": row["last_activity_at"], "my_role": row["my_role"],
    }
    now = to_iso(now_dt())
    active = row["status"] in workflow.ACTIVE
    d["overdue"] = bool(row["due_at"] and row["due_at"] < now and active)
    d["stale"] = bool(
        row["status"] in ("open", "in_progress", "blocked")
        and row["last_activity_at"] < to_iso(now_dt() - timedelta(days=STALE_AFTER_DAYS))
    )
    if full:
        d["description"] = row["description"]
    role = row["my_role"]
    d["can"] = {
        "comment": authz.can_comment(role),
        "edit": authz.can_edit(row, role, uid),
        "set_priority": authz.can_set_priority(row, role),
        "claim": authz.can_claim(row, role),
        "release": authz.can_release(row, role, uid),
        "assign": authz.can_assign(row, role),
        "review": authz.can_review(row, role, uid),
        "transitions": workflow.allowed_transitions(row, role, uid),
    }
    return d


def fetch_item(db, me, item_id):
    """Returns the row only if `me` is a member of the item's team, else None (-> 404)."""
    return db.execute(f"SELECT {_COLS} {_BASE_FROM} WHERE w.id = :id", {"me": me, "id": item_id}).fetchone()


def last_change(db, item_id):
    r = db.execute(
        "SELECT e.type, e.created_at, u.name AS actor_name FROM events e LEFT JOIN users u ON u.id = e.actor_id "
        "WHERE e.item_id = ? ORDER BY e.id DESC LIMIT 1", (item_id,)).fetchone()
    return dict(r) if r else None


# --- list / search ---------------------------------------------------------------------------
def _enc(tup):
    return base64.urlsafe_b64encode(json.dumps(tup).encode()).decode()


def _dec(s, n):
    try:
        t = json.loads(base64.urlsafe_b64decode(s.encode()))
        if not isinstance(t, list) or len(t) != n:
            raise ValueError
        return t
    except Exception:
        raise validation("cursor is invalid", "cursor")


def _csv(value, allowed, field):
    vals = [v for v in (value or "").split(",") if v]
    for v in vals:
        if v not in allowed:
            raise validation(f"{field} has invalid value '{v}'", field)
    return vals


def list_items(db, me, args):
    p = _params(me)
    where = []
    view = args.get("view") or "all"
    if view not in VIEWS:
        raise validation("unknown view", "view")
    where.append(f"({VIEWS[view]})")

    if args.get("team_id"):
        try:
            p["team_id"] = int(args["team_id"])
        except ValueError:
            raise validation("team_id must be an integer", "team_id")
        where.append("w.team_id = :team_id")

    for field, allowed, col in (("status", workflow.STATUSES, "w.status"), ("priority", workflow.PRIORITIES, "w.priority")):
        vals = _csv(args.get(field), allowed, field)
        if vals:
            names = []
            for i, v in enumerate(vals):
                p[f"{field}{i}"] = v
                names.append(f":{field}{i}")
            where.append(f"{col} IN ({','.join(names)})")

    asg = args.get("assignee")
    if asg == "me":
        where.append("w.assignee_id = :me")
    elif asg == "none":
        where.append("w.assignee_id IS NULL")
    elif asg:
        try:
            p["assignee"] = int(asg)
        except ValueError:
            raise validation("assignee must be 'me', 'none' or a user id", "assignee")
        where.append("w.assignee_id = :assignee")

    if args.get("ids"):
        try:
            ids = [int(x) for x in args["ids"].split(",")][:100]
        except ValueError:
            raise validation("ids must be integers", "ids")
        for i, v in enumerate(ids):
            p[f"id{i}"] = v
        where.append(f"w.id IN ({','.join(':id%d' % i for i in range(len(ids))) or 'NULL'})")

    q = (args.get("q") or "").strip()
    if q:
        tokens = re.findall(r"\w+", q)[:8]
        clauses = []
        if re.fullmatch(r"#?\d+", q):
            p["qid"] = int(q.lstrip("#"))
            clauses.append("w.id = :qid")
        if tokens:
            p["fts"] = " ".join(f'"{t}"*' for t in tokens)   # quoted prefix terms: no FTS syntax injection
            clauses.append("w.id IN (SELECT rowid FROM items_fts WHERE items_fts MATCH :fts)")
        where.append("(" + " OR ".join(clauses or ["0"]) + ")")

    sort = args.get("sort") or "updated"
    if sort not in SORTS:
        raise validation("unknown sort", "sort")
    cols = SORTS[sort]
    if args.get("cursor"):
        vals = _dec(args["cursor"], len(cols))
        for i, v in enumerate(vals):
            p[f"c{i}"] = v
        where.append(f"({', '.join(cols)}) < ({', '.join(':c%d' % i for i in range(len(cols)))})")

    try:
        limit = min(max(int(args.get("limit", 50)), 1), 100)
    except ValueError:
        raise validation("limit must be an integer", "limit")
    p["limit"] = limit + 1   # fetch one extra row to learn whether there is a next page

    sql = (f"SELECT {_COLS} {_BASE_FROM} WHERE {' AND '.join(where)} "
           f"ORDER BY {', '.join(c + ' DESC' for c in cols)} LIMIT :limit")
    rows = db.execute(sql, p).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    nxt = None
    if has_more and rows:
        last = rows[-1]
        nxt = _enc([last[c.split(".")[1]] for c in cols])
    return {"items": [serialize_item(r, me) for r in rows], "next_cursor": nxt, "has_more": has_more}


# --- dashboard -------------------------------------------------------------------------------
def dashboard(db, me):
    p = _params(me)
    sums = ", ".join(f"COALESCE(SUM(CASE WHEN {frag} THEN 1 ELSE 0 END),0) AS {name}"
                     for name, frag in VIEWS.items() if name != "all")
    counts = dict(db.execute(f"SELECT {sums} {_BASE_FROM}", p).fetchone())
    by_team = db.execute(
        f"""SELECT w.team_id, t.name AS team_name, m.role AS my_role,
                   SUM(w.status='open') AS open, SUM(w.status='in_progress') AS in_progress,
                   SUM(w.status='blocked') AS blocked, SUM(w.status='awaiting_approval') AS awaiting_approval,
                   SUM(w.assignee_id IS NULL AND w.status IN ('open','blocked')) AS unassigned
            {_BASE_FROM} WHERE {VIEWS['active']} GROUP BY w.team_id ORDER BY t.name""", p).fetchall()
    unread = db.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL", (me,)).fetchone()[0]
    return {"counts": counts, "teams": [dict(r) for r in by_team], "unread_notifications": unread}


# --- history ---------------------------------------------------------------------------------
def item_events(db, item_id, before=None, limit=30):
    limit = min(max(limit, 1), 100)
    args = [item_id]
    cond = ""
    if before:
        cond = "AND e.id < ?"
        args.append(before)
    rows = db.execute(
        f"SELECT e.id, e.type, e.data, e.created_at, e.actor_id, u.name AS actor_name FROM events e "
        f"LEFT JOIN users u ON u.id = e.actor_id WHERE e.item_id = ? {cond} ORDER BY e.id DESC LIMIT ?",
        (*args, limit + 1)).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    ev = [{"id": r["id"], "type": r["type"], "data": json.loads(r["data"]), "created_at": r["created_at"],
           "actor_id": r["actor_id"], "actor_name": r["actor_name"] or "System"} for r in rows]
    return {"events": ev, "next_before": ev[-1]["id"] if more and ev else None}


# --- change feed (stale-view detection) --------------------------------------------------------
MAX_FEED = 500


def changes(db, me, since):
    head = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
    if since is None:
        return {"cursor": head, "item_ids": [], "reset": False}
    rows = db.execute(
        """SELECT e.id, e.item_id FROM events e
           JOIN work_items w ON w.id = e.item_id
           JOIN memberships m ON m.team_id = w.team_id AND m.user_id = :me
           WHERE e.id > :since ORDER BY e.id LIMIT :lim""", {"me": me, "since": since, "lim": MAX_FEED + 1}).fetchall()
    if len(rows) > MAX_FEED:       # client too far behind: tell it to reload instead of replaying
        return {"cursor": head, "item_ids": [], "reset": True}
    cursor = rows[-1]["id"] if rows else max(since, 0)
    ids = list(dict.fromkeys(r["item_id"] for r in rows))
    return {"cursor": max(cursor, since), "item_ids": ids, "reset": False}


def notifications(db, me, limit=30):
    rows = db.execute(
        "SELECT id, item_id, kind, message, read_at, created_at FROM notifications "
        "WHERE user_id=? ORDER BY id DESC LIMIT ?", (me, limit)).fetchall()
    unread = db.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL", (me,)).fetchone()[0]
    return {"notifications": [dict(r) for r in rows], "unread": unread}


def not_found():
    return ApiError(404, "not_found", "Work item not found")
