"""HTTP layer: authentication, idempotent write wrapper, routes. Business rules live in services.py."""
import hashlib
import json
import secrets
from functools import wraps

from flask import Blueprint, Response, current_app, g, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

from . import authz, queries, services
from .common import ApiError, clean_str, iso_in, now_iso, validation
from .db import get_db

bp = Blueprint("api", __name__, url_prefix="/api")
SESSION_TTL = 12 * 3600
_DUMMY_HASH = generate_password_hash("x", method="pbkdf2:sha256:1000")   # equalise timing for unknown users


def hash_password(pw):
    return generate_password_hash(pw, method=current_app.config["HASH_METHOD"])


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


# --- authentication --------------------------------------------------------------------------
@bp.before_request
def authenticate():
    if request.endpoint in ("api.login", "api.health"):
        return
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise ApiError(401, "unauthenticated", "Sign in to continue")
    row = get_db().execute(
        "SELECT u.id, u.email, u.name, u.is_admin FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ?", (_sha(header[7:]), now_iso())).fetchone()
    if row is None:
        raise ApiError(401, "unauthenticated", "Session expired; sign in again")
    g.user = dict(row)


def body_json():
    data = request.get_json(silent=True)
    if data is None and request.data:
        raise validation("Body must be valid JSON")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise validation("Body must be a JSON object")
    return data


# --- idempotent write wrapper ----------------------------------------------------------------
def execute_write(fn):
    """Runs fn(db) -> (status, body) in ONE BEGIN IMMEDIATE transaction.

    If the client sent `Idempotency-Key`, the stored response of a previous identical request is replayed
    instead of executing again. The key lookup and the action share a transaction, and BEGIN IMMEDIATE
    serialises writers, so two simultaneous duplicates cannot both execute.
    Only successful results are stored: failures (409/403/...) are safe to retry and should be re-evaluated.
    """
    db = get_db()
    key = request.headers.get("Idempotency-Key")
    if key is not None and not (1 <= len(key) <= 120):
        raise validation("Idempotency-Key must be 1-120 characters")
    fp = _sha(f"{request.method} {request.path} {request.get_data(as_text=True)}")
    uid = g.user["id"]
    db.execute("BEGIN IMMEDIATE")
    try:
        if key:
            prev = db.execute("SELECT fingerprint,status_code,response_body FROM idempotency_keys WHERE user_id=? AND key=?",
                              (uid, key)).fetchone()
            if prev:
                db.execute("ROLLBACK")
                if prev["fingerprint"] != fp:
                    raise ApiError(422, "idempotency_key_reuse", "This Idempotency-Key was already used for a different request")
                resp = Response(prev["response_body"], status=prev["status_code"], mimetype="application/json")
                resp.headers["Idempotent-Replayed"] = "true"
                return resp
        status, body = fn(db)
        payload = json.dumps(body)
        if key:
            db.execute("INSERT INTO idempotency_keys VALUES (?,?,?,?,?,?)", (uid, key, fp, status, payload, now_iso()))
        db.execute("COMMIT")
        return Response(payload, status=status, mimetype="application/json")
    except ApiError:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


def write(service):
    """Decorator for POST/PATCH routes that call services.<fn>(db, user, [item_id,] body)."""
    def deco(view):
        @wraps(view)
        def inner(**kw):
            body = body_json()
            return execute_write(lambda db: service(db, g.user, *kw.values(), body))
        return inner
    return deco


# --- public ----------------------------------------------------------------------------------
@bp.get("/health")
def health():
    return jsonify(ok=True)


@bp.post("/auth/login")
def login():
    body = body_json()
    email = clean_str(body, "email", required=True, max_len=200)
    pw = body.get("password")
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    ok = check_password_hash(u["password_hash"] if u else _DUMMY_HASH, pw if isinstance(pw, str) else "")
    if not (u and ok):
        raise ApiError(401, "invalid_credentials", "Email or password is incorrect")
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO sessions VALUES (?,?,?)", (_sha(token), u["id"], iso_in(SESSION_TTL)))
    return jsonify(token=token, user=_user_payload(db, u["id"]))


@bp.post("/auth/logout")
def logout():
    get_db().execute("DELETE FROM sessions WHERE token_hash=?", (_sha(request.headers["Authorization"][7:]),))
    return jsonify(ok=True)


def _user_payload(db, uid):
    u = db.execute("SELECT id,email,name,is_admin FROM users WHERE id=?", (uid,)).fetchone()
    teams = db.execute("SELECT t.id, t.name, m.role FROM memberships m JOIN teams t ON t.id=m.team_id "
                       "WHERE m.user_id=? ORDER BY t.name", (uid,)).fetchall()
    return {**dict(u), "is_admin": bool(u["is_admin"]), "teams": [dict(t) for t in teams]}


@bp.get("/me")
def me():
    return jsonify(_user_payload(get_db(), g.user["id"]))


# --- teams -----------------------------------------------------------------------------------
@bp.get("/teams/<int:team_id>/members")
def team_members(team_id):
    db = get_db()
    if db.execute("SELECT 1 FROM memberships WHERE team_id=? AND user_id=?", (team_id, g.user["id"])).fetchone() is None:
        raise ApiError(404, "not_found", "Team not found")
    rows = db.execute("SELECT u.id, u.name, u.email, m.role FROM memberships m JOIN users u ON u.id=m.user_id "
                      "WHERE m.team_id=? ORDER BY u.name", (team_id,)).fetchall()
    return jsonify(members=[dict(r) for r in rows])


def _can_manage_team(db, team_id):
    if g.user["is_admin"]:
        return True
    r = db.execute("SELECT role FROM memberships WHERE team_id=? AND user_id=?", (team_id, g.user["id"])).fetchone()
    return r is not None and r["role"] == "admin"


@bp.put("/teams/<int:team_id>/members")
def set_member(team_id):
    db = get_db()
    if not _can_manage_team(db, team_id):
        raise ApiError(403, "forbidden", "Only team admins can manage membership")
    body = body_json()
    role = body.get("role")
    if role not in authz.RANK:
        raise validation("role must be one of viewer, member, lead, admin", "role")
    email = clean_str(body, "email", required=True)
    u = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if u is None:
        raise ApiError(404, "not_found", "No user with that email")
    # Never leave a team without an admin.
    cur = db.execute("SELECT role FROM memberships WHERE team_id=? AND user_id=?", (team_id, u["id"])).fetchone()
    if cur and cur["role"] == "admin" and role != "admin":
        n = db.execute("SELECT COUNT(*) FROM memberships WHERE team_id=? AND role='admin'", (team_id,)).fetchone()[0]
        if n <= 1:
            raise ApiError(409, "last_admin", "A team must keep at least one admin")
    db.execute("INSERT INTO memberships(team_id,user_id,role) VALUES (?,?,?) "
               "ON CONFLICT(team_id,user_id) DO UPDATE SET role=excluded.role", (team_id, u["id"], role))
    return jsonify(ok=True)


@bp.delete("/teams/<int:team_id>/members/<int:user_id>")
def remove_member(team_id, user_id):
    db = get_db()
    if not _can_manage_team(db, team_id):
        raise ApiError(403, "forbidden", "Only team admins can manage membership")
    cur = db.execute("SELECT role FROM memberships WHERE team_id=? AND user_id=?", (team_id, user_id)).fetchone()
    if cur and cur["role"] == "admin":
        n = db.execute("SELECT COUNT(*) FROM memberships WHERE team_id=? AND role='admin'", (team_id,)).fetchone()[0]
        if n <= 1:
            raise ApiError(409, "last_admin", "A team must keep at least one admin")
    # Active items they hold are released so work doesn't stay owned by someone who lost access.
    # (awaiting_approval items keep their owner: the DB invariant requires one, and a lead will review them.)
    db.execute("BEGIN IMMEDIATE")
    try:
        now = now_iso()
        for it in db.execute("SELECT id, status FROM work_items WHERE team_id=? AND assignee_id=? "
                             "AND status IN ('open','in_progress','blocked')", (team_id, user_id)).fetchall():
            db.execute("UPDATE work_items SET assignee_id=NULL, status=?, version=version+1, updated_at=?, last_activity_at=? WHERE id=?",
                       ("open" if it["status"] == "in_progress" else it["status"], now, now, it["id"]))
            db.execute("INSERT INTO events(item_id,actor_id,type,data,created_at) VALUES (?,?,?,?,?)",
                       (it["id"], g.user["id"], "assigned", json.dumps({"from": user_id, "to": None, "via": "removed_from_team"}), now))
        db.execute("DELETE FROM memberships WHERE team_id=? AND user_id=?", (team_id, user_id))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return jsonify(ok=True)


# --- dashboard / feed / notifications --------------------------------------------------------
@bp.get("/dashboard")
def dashboard():
    return jsonify(queries.dashboard(get_db(), g.user["id"]))


@bp.get("/changes")
def changes():
    since = request.args.get("since")
    try:
        since = int(since) if since not in (None, "") else None
    except ValueError:
        raise validation("since must be an integer", "since")
    return jsonify(queries.changes(get_db(), g.user["id"], since))


@bp.get("/notifications")
def notifications():
    return jsonify(queries.notifications(get_db(), g.user["id"]))


@bp.post("/notifications/read")
def notifications_read():
    body = body_json()
    db = get_db()
    ids = body.get("ids")
    if ids is None:
        db.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL", (now_iso(), g.user["id"]))
    else:
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            raise validation("ids must be a list of integers", "ids")
        db.execute(f"UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL AND id IN ({','.join('?' * len(ids)) or 'NULL'})",
                   (now_iso(), g.user["id"], *ids))
    return jsonify(ok=True)


# --- work items ------------------------------------------------------------------------------
@bp.get("/items")
def items_list():
    return jsonify(queries.list_items(get_db(), g.user["id"], request.args))


@bp.post("/items")
@write(services.create_item)
def items_create():
    pass


@bp.get("/items/<int:item_id>")
def items_get(item_id):
    row = queries.fetch_item(get_db(), g.user["id"], item_id)
    if row is None:
        raise queries.not_found()
    return jsonify(item=queries.serialize_item(row, g.user["id"], full=True))


@bp.get("/items/<int:item_id>/events")
def items_events(item_id):
    db = get_db()
    if queries.fetch_item(db, g.user["id"], item_id) is None:
        raise queries.not_found()
    try:
        before = int(request.args["before"]) if request.args.get("before") else None
        limit = int(request.args.get("limit", 30))
    except ValueError:
        raise validation("before/limit must be integers")
    return jsonify(queries.item_events(db, item_id, before, limit))


@bp.patch("/items/<int:item_id>")
@write(services.update_item)
def items_update(item_id):
    pass


@bp.post("/items/<int:item_id>/claim")
@write(services.claim)
def items_claim(item_id):
    pass


@bp.post("/items/<int:item_id>/release")
@write(services.release)
def items_release(item_id):
    pass


@bp.post("/items/<int:item_id>/assign")
@write(services.assign)
def items_assign(item_id):
    pass


@bp.post("/items/<int:item_id>/transition")
@write(services.transition)
def items_transition(item_id):
    pass


@bp.post("/items/<int:item_id>/approve")
@write(services.approve)
def items_approve(item_id):
    pass


@bp.post("/items/<int:item_id>/reject")
@write(services.reject)
def items_reject(item_id):
    pass


@bp.post("/items/<int:item_id>/comments")
@write(services.comment)
def items_comment(item_id):
    pass
