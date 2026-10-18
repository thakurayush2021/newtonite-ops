"""Asynchronous processing: a transactional outbox + at-least-once job runner.

Design
  * Jobs are rows in `jobs`, inserted in the SAME transaction as the state change that caused them.
    So "action committed" <=> "job exists". No lost notifications, no phantom ones.
  * A worker claims a job with an atomic UPDATE ... RETURNING and a *lease* (locked_until). If the worker
    dies mid-job the lease expires and another worker re-claims it (=> a job may run more than once).
  * Therefore handlers MUST be idempotent. Ours write notifications with INSERT OR IGNORE against a
    UNIQUE(user_id, dedupe_key), so a repeated run produces no duplicates.
  * Failures retry with exponential backoff; after max_attempts the job becomes 'dead' (kept for inspection).
  * The user-facing request never waits for, or fails because of, any of this.
"""
import json
import logging
import threading
import time
from datetime import timedelta

from . import db as dbmod
from .common import now_dt, now_iso, to_iso
from .queries import STALE_AFTER_DAYS

log = logging.getLogger("worker")
HANDLERS = {}
LEASE_SECONDS = 60


def handler(kind):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


def enqueue(db, kind, payload, dedupe_key=None, delay=0, max_attempts=5):
    """Idempotent when dedupe_key is given. Call inside the caller's transaction."""
    db.execute(
        "INSERT OR IGNORE INTO jobs(kind,payload,run_at,max_attempts,dedupe_key,created_at) VALUES (?,?,?,?,?,?)",
        (kind, json.dumps(payload), to_iso(now_dt() + timedelta(seconds=delay)), max_attempts, dedupe_key, now_iso()))


# --- job execution ---------------------------------------------------------------------------
def claim(db, now=None):
    now = now or now_dt()
    return db.execute(
        """UPDATE jobs SET status='running', attempts=attempts+1, locked_until=:lease
           WHERE id = (SELECT id FROM jobs
                       WHERE (status='pending' AND run_at <= :now) OR (status='running' AND locked_until < :now)
                       ORDER BY run_at, id LIMIT 1)
           RETURNING *""",
        {"now": to_iso(now), "lease": to_iso(now + timedelta(seconds=LEASE_SECONDS))}).fetchone()


def process_one(db, now=None):
    """Runs a single ready job. Returns True if a job was claimed."""
    now = now or now_dt()
    job = claim(db, now)
    if job is None:
        return False
    if job["attempts"] > job["max_attempts"]:   # repeatedly crashed workers: stop re-claiming it
        db.execute("UPDATE jobs SET status='dead', last_error=COALESCE(last_error,'lease expired too many times') WHERE id=?",
                   (job["id"],))
        return True
    try:
        fn = HANDLERS[job["kind"]]
        fn(db, json.loads(job["payload"]), now)
        db.execute("UPDATE jobs SET status='done', locked_until=NULL, last_error=NULL WHERE id=?", (job["id"],))
    except Exception as exc:  # noqa: BLE001 - a job failure must never crash the worker
        log.warning("job %s (%s) failed: %s", job["id"], job["kind"], exc)
        if job["attempts"] >= job["max_attempts"]:
            db.execute("UPDATE jobs SET status='dead', locked_until=NULL, last_error=? WHERE id=?",
                       (str(exc)[:500], job["id"]))
        else:
            backoff = min(300, 2 ** job["attempts"])
            db.execute("UPDATE jobs SET status='pending', locked_until=NULL, last_error=?, run_at=? WHERE id=?",
                       (str(exc)[:500], to_iso(now + timedelta(seconds=backoff)), job["id"]))
    return True


def drain(db, now=None, limit=10_000):
    n = 0
    while n < limit and process_one(db, now):
        n += 1
    return n


# --- handlers --------------------------------------------------------------------------------
def _members_only(db, team_id, user_ids, exclude):
    ids = [u for u in set(user_ids) if u is not None and u != exclude]
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    return [r[0] for r in db.execute(
        f"SELECT user_id FROM memberships WHERE team_id=? AND user_id IN ({q})", (team_id, *ids))]


def _leads(db, team_id):
    return [r[0] for r in db.execute("SELECT user_id FROM memberships WHERE team_id=? AND role IN ('lead','admin')", (team_id,))]


def _notify(db, user_ids, item_id, kind, message, dedupe_key, now):
    for uid in user_ids:
        db.execute(
            "INSERT OR IGNORE INTO notifications(user_id,item_id,kind,message,dedupe_key,created_at) VALUES (?,?,?,?,?,?)",
            (uid, item_id, kind, message, f"{dedupe_key}", to_iso(now)))


@handler("notify_event")
def notify_event(db, payload, now):
    ev = db.execute(
        "SELECT e.*, u.name AS actor_name FROM events e LEFT JOIN users u ON u.id=e.actor_id WHERE e.id=?",
        (payload["event_id"],)).fetchone()
    if ev is None:
        return
    item = db.execute("SELECT * FROM work_items WHERE id=?", (ev["item_id"],)).fetchone()
    data = json.loads(ev["data"])
    actor, who, t = ev["actor_id"], ev["actor_name"] or "System", ev["type"]
    title = f"#{item['id']} {item['title']}"
    commenters = [r[0] for r in db.execute(
        "SELECT DISTINCT actor_id FROM events WHERE item_id=? AND type='comment'", (item["id"],))]
    participants = [item["creator_id"], item["assignee_id"], *commenters]

    if t == "created":
        recipients = _leads(db, item["team_id"]) if item["priority"] in ("high", "urgent") else []
        msg = f"{who} opened a {item['priority']} item: {title}"
    elif t == "assigned":
        recipients = [data.get("to"), data.get("from"), *participants]   # requester wants to know it was picked up
        msg = (f"{who} assigned {title} to {data.get('to_name')}" if data.get("to")
               else f"{who} released {title}")
    elif t == "approval_requested":
        recipients = _leads(db, item["team_id"])
        msg = f"{who} requests approval to resolve {title}"
    elif t in ("approved", "rejected"):
        recipients = participants
        msg = f"{who} {t} the resolution of {title}"
    elif t == "status_changed":
        recipients = participants
        msg = f"{who} moved {title} to {data.get('to', '').replace('_', ' ')}"
    elif t == "priority_changed":
        recipients = participants
        msg = f"{who} changed priority of {title} to {data.get('to')}"
    elif t == "comment":
        recipients = participants
        msg = f"{who} commented on {title}"
    else:
        recipients = participants
        msg = f"{who} updated {title}"

    recipients = _members_only(db, item["team_id"], recipients, exclude=actor)
    db.execute("BEGIN IMMEDIATE")
    try:
        _notify(db, recipients, item["id"], t, msg, f"event:{ev['id']}", now)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


@handler("stale_sweep")
def stale_sweep(db, payload, now):
    """Surface forgotten work: active items with no activity for N days nag the assignee (or the leads)."""
    cutoff = to_iso(now - timedelta(days=STALE_AFTER_DAYS))
    day = now.strftime("%Y%m%d")
    rows = db.execute(
        "SELECT id, team_id, title, assignee_id FROM work_items "
        "WHERE status IN ('open','in_progress','blocked') AND last_activity_at < ? LIMIT 5000", (cutoff,)).fetchall()
    db.execute("BEGIN IMMEDIATE")
    try:
        for r in rows:
            targets = [r["assignee_id"]] if r["assignee_id"] else _leads(db, r["team_id"])
            _notify(db, targets, r["id"], "stale", f"No activity for {STALE_AFTER_DAYS}+ days on #{r['id']} {r['title']}",
                    f"stale:{r['id']}:{day}", now)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


# --- runner ----------------------------------------------------------------------------------
def run_forever(db_path, stop: threading.Event, poll=0.5, sweep_every=900):
    conn = dbmod.connect(db_path)
    last_sweep = 0.0
    while not stop.is_set():
        try:
            if time.time() - last_sweep > sweep_every:
                last_sweep = time.time()
                bucket = now_dt().strftime("%Y%m%d%H")
                enqueue(conn, "stale_sweep", {}, dedupe_key=f"stale_sweep:{bucket}")
            if not process_one(conn):
                stop.wait(poll)
        except Exception:  # noqa: BLE001
            log.exception("worker loop error")
            stop.wait(2)
    conn.close()


def start_thread(db_path):
    stop = threading.Event()
    th = threading.Thread(target=run_forever, args=(db_path, stop), daemon=True, name="job-worker")
    th.start()
    return stop, th


if __name__ == "__main__":   # standalone: python -m app.worker  (run N of these for more throughput)
    import os
    logging.basicConfig(level=logging.INFO)
    run_forever(os.environ.get("DATABASE", "newtonite.db"), threading.Event())
