"""Write-side business logic.

Every function runs INSIDE a transaction opened by api.execute_write (BEGIN IMMEDIATE) and returns
(http_status, body). Correctness primitives, in order of importance:

  1. Conditional UPDATEs (`WHERE version = ?`, `WHERE assignee_id IS NULL`): the write itself is the
     compare-and-swap, so correctness doesn't depend on a separate read being fresh.
  2. Event + outbox job written in the same transaction as the state change.
  3. Workflow/authorization checks via the shared predicates in authz.py / workflow.py.
"""
import json

from . import authz, queries, workflow
from .common import ApiError, clean_str, now_iso, parse_due, validation
from .worker import enqueue


# --- helpers ---------------------------------------------------------------------------------
def _fetch(db, user, item_id):
    row = queries.fetch_item(db, user["id"], item_id)
    if row is None:
        raise queries.not_found()          # same answer for "missing" and "not your team"
    return row


def _expected(body):
    v = body.get("expected_version")
    if isinstance(v, bool) or not isinstance(v, int):
        raise validation("expected_version (integer) is required: send the version you were looking at", "expected_version")
    return v


def _conflict(db, user, item_id, message):
    cur = _fetch(db, user, item_id)
    return ApiError(409, "version_conflict", message, {
        "current": queries.serialize_item(cur, user["id"], full=True),
        "last_change": queries.last_change(db, item_id),
    })


def _check_version(db, user, item, expected):
    if item["version"] != expected:
        raise _conflict(db, user, item["id"], "This item changed since you loaded it")


def _emit(db, item_id, actor_id, etype, data, now):
    cur = db.execute("INSERT INTO events(item_id,actor_id,type,data,created_at) VALUES (?,?,?,?,?)",
                     (item_id, actor_id, etype, json.dumps(data), now))
    eid = cur.lastrowid
    enqueue(db, "notify_event", {"event_id": eid}, dedupe_key=f"notify:{eid}")   # transactional outbox
    return eid


def _cas_update(db, user, item, expected, sets, now, touch_version=True):
    """Compare-and-swap on version. Raises 409 with the current state if someone else got there first."""
    cols = ", ".join(f"{k} = ?" for k in sets)
    cur = db.execute(
        f"UPDATE work_items SET {cols}, version = version + 1, updated_at = ?, last_activity_at = ? "
        f"WHERE id = ? AND version = ?", (*sets.values(), now, now, item["id"], expected))
    if cur.rowcount != 1:
        raise _conflict(db, user, item["id"], "This item changed since you loaded it")


def _result(db, user, item_id, status=200, **extra):
    row = _fetch(db, user, item_id)
    return status, {"item": queries.serialize_item(row, user["id"], full=True), **extra}


def _team_role(db, user_id, team_id):
    r = db.execute("SELECT role FROM memberships WHERE team_id=? AND user_id=?", (team_id, user_id)).fetchone()
    return r["role"] if r else None


# --- create ----------------------------------------------------------------------------------
def create_item(db, user, body):
    try:
        team_id = int(body.get("team_id"))
    except (TypeError, ValueError):
        raise validation("team_id is required", "team_id")
    role = _team_role(db, user["id"], team_id)
    if role is None:
        raise ApiError(403, "forbidden", "You are not a member of that team")
    authz.require_role(role, "member", "Viewers cannot create work items")

    title = clean_str(body, "title", required=True, max_len=200)
    desc = clean_str(body, "description", max_len=10_000, default="") or ""
    priority = body.get("priority", "normal")
    if priority not in workflow.PRIORITIES:
        raise validation(f"priority must be one of {', '.join(workflow.PRIORITIES)}", "priority")
    due = parse_due(body.get("due_at"))
    req_appr = body.get("requires_approval", False)
    if not isinstance(req_appr, bool):
        raise validation("requires_approval must be a boolean", "requires_approval")

    now = now_iso()
    cur = db.execute(
        "INSERT INTO work_items(team_id,title,description,priority,prank,creator_id,due_at,requires_approval,"
        "created_at,updated_at,last_activity_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (team_id, title, desc, priority, workflow.PRANK[priority], user["id"], due, int(req_appr), now, now, now))
    item_id = cur.lastrowid
    _emit(db, item_id, user["id"], "created", {"title": title, "priority": priority, "due_at": due}, now)
    return _result(db, user, item_id, 201)


# --- edit ------------------------------------------------------------------------------------
_EDITABLE = ("title", "description", "due_at", "priority", "requires_approval")


def update_item(db, user, item_id, body):
    item = _fetch(db, user, item_id)
    role, uid = item["my_role"], user["id"]
    expected = _expected(body)
    unknown = set(body) - set(_EDITABLE) - {"expected_version"}
    if unknown:
        raise validation(f"cannot edit: {', '.join(sorted(unknown))}")
    if authz.is_closed(item):
        raise ApiError(409, "item_closed", "Closed items are read-only; reopen the item first")

    new = {}
    if "title" in body:
        new["title"] = clean_str(body, "title", required=True, max_len=200)
    if "description" in body:
        new["description"] = clean_str(body, "description", max_len=10_000, default="") or ""
    if "due_at" in body:
        new["due_at"] = parse_due(body["due_at"])
    if "priority" in body:
        if body["priority"] not in workflow.PRIORITIES:
            raise validation(f"priority must be one of {', '.join(workflow.PRIORITIES)}", "priority")
        new["priority"] = body["priority"]
    if "requires_approval" in body:
        if not isinstance(body["requires_approval"], bool):
            raise validation("requires_approval must be a boolean", "requires_approval")
        new["requires_approval"] = int(body["requires_approval"])

    changes = {k: v for k, v in new.items() if item[k] != v}
    # Per-field authorization (resource-level): content vs. triage decisions.
    if {"title", "description", "due_at"} & set(changes) and not authz.can_edit(item, role, uid):
        raise ApiError(403, "forbidden", "Only the creator, the assignee or a lead can edit this item")
    if {"priority", "requires_approval"} & set(changes) and not authz.can_set_priority(item, role):
        raise ApiError(403, "forbidden", "Only a lead can change priority or approval requirements")
    if "requires_approval" in changes and item["status"] in ("awaiting_approval", "resolved"):
        raise ApiError(409, "invalid_state", "Approval requirement cannot change once approval was requested")

    _check_version(db, user, item, expected)
    if not changes:                                  # no-op PATCH: nothing to record, no version bump
        return _result(db, user, item_id)

    sets = dict(changes)
    if "priority" in sets:
        sets["prank"] = workflow.PRANK[sets["priority"]]
    now = now_iso()
    _cas_update(db, user, item, expected, sets, now)

    if "priority" in changes:
        _emit(db, item_id, uid, "priority_changed", {"from": item["priority"], "to": changes["priority"]}, now)
    content = {k: {"from": (item[k] or "")[:2000] if isinstance(item[k], str) else item[k],
                   "to": v[:2000] if isinstance(v, str) else v}
               for k, v in changes.items() if k != "priority"}
    if content:
        _emit(db, item_id, uid, "updated", {"changes": content}, now)
    return _result(db, user, item_id)


# --- ownership -------------------------------------------------------------------------------
def claim(db, user, item_id, body):
    """Atomic claim: UPDATE ... WHERE assignee_id IS NULL. Exactly one of N concurrent claimers wins."""
    item = _fetch(db, user, item_id)
    role, uid = item["my_role"], user["id"]
    authz.require_role(role, "member", "Viewers cannot claim work")
    if authz.is_closed(item):
        raise ApiError(409, "item_closed", "Closed items cannot be claimed")
    if item["assignee_id"] == uid:                   # repeated action: succeed without a second event
        return _result(db, user, item_id, 200, already=True)
    now = now_iso()
    cur = db.execute(
        "UPDATE work_items SET assignee_id=?, version=version+1, updated_at=?, last_activity_at=? "
        "WHERE id=? AND assignee_id IS NULL AND status != 'closed'", (uid, now, now, item_id))
    if cur.rowcount != 1:
        fresh = _fetch(db, user, item_id)
        raise ApiError(409, "already_claimed", f"{fresh['assignee_name'] or 'Someone else'} already took this item",
                       {"current": queries.serialize_item(fresh, uid, full=True)})
    _emit(db, item_id, uid, "assigned",
          {"from": None, "to": uid, "to_name": user["name"], "via": "claim"}, now)
    return _result(db, user, item_id)


def _set_assignee(db, user, item, expected, new_id, new_name, via, now):
    sets = {"assignee_id": new_id}
    if new_id is None and item["status"] == "in_progress":
        sets["status"] = "open"                      # invariant: in_progress always has an owner
    _cas_update(db, user, item, expected, sets, now)
    _emit(db, item["id"], user["id"], "assigned",
          {"from": item["assignee_id"], "from_name": item["assignee_name"], "to": new_id, "to_name": new_name, "via": via}, now)
    if "status" in sets:
        _emit(db, item["id"], user["id"], "status_changed",
              {"from": "in_progress", "to": "open", "reason": "released"}, now)


def release(db, user, item_id, body):
    item = _fetch(db, user, item_id)
    expected = _expected(body)
    if item["assignee_id"] is None:
        return _result(db, user, item_id, 200, already=True)
    if not authz.can_release(item, item["my_role"], user["id"]):
        raise ApiError(403, "forbidden", "Only the assignee or a lead can release this item")
    _check_version(db, user, item, expected)
    _set_assignee(db, user, item, expected, None, None, "release", now_iso())
    return _result(db, user, item_id)


def assign(db, user, item_id, body):
    item = _fetch(db, user, item_id)
    expected = _expected(body)
    if not authz.can_assign(item, item["my_role"]):
        raise ApiError(403, "forbidden", "Only a lead can assign work to others")
    target = body.get("assignee_id")
    if target is None:
        raise validation("assignee_id is required (use /release to unassign)", "assignee_id")
    if isinstance(target, bool) or not isinstance(target, int):
        raise validation("assignee_id must be a user id", "assignee_id")
    row = db.execute("SELECT u.name, m.role FROM users u JOIN memberships m ON m.user_id=u.id AND m.team_id=? "
                     "WHERE u.id=?", (item["team_id"], target)).fetchone()
    if row is None or not authz.at_least(row["role"], "member"):
        raise ApiError(409, "invalid_assignee", "Assignee must be a non-viewer member of the item's team")
    _check_version(db, user, item, expected)
    if item["assignee_id"] == target:
        return _result(db, user, item_id, 200, already=True)
    _set_assignee(db, user, item, expected, target, row["name"], "assign", now_iso())
    return _result(db, user, item_id)


# --- workflow --------------------------------------------------------------------------------
def transition(db, user, item_id, body):
    item = _fetch(db, user, item_id)
    role, uid = item["my_role"], user["id"]
    expected = _expected(body)
    to = body.get("to")
    if to not in workflow.STATUSES:
        raise validation(f"to must be one of {', '.join(workflow.STATUSES)}", "to")
    if to == item["status"]:
        raise ApiError(409, "invalid_transition", f"Item is already {to.replace('_', ' ')}",
                       {"allowed": [t["to"] for t in workflow.allowed_transitions(item, role, uid)]})
    if to not in workflow.graph_targets(item):
        hint = ""
        if item["status"] == "awaiting_approval":
            hint = " (awaiting approval: a lead must approve or reject)"
        elif to == "resolved" and item["requires_approval"]:
            hint = " (this item requires approval: submit it for approval instead)"
        raise ApiError(409, "invalid_transition",
                       f"Cannot move from {item['status'].replace('_', ' ')} to {to.replace('_', ' ')}{hint}",
                       {"allowed": [t["to"] for t in workflow.allowed_transitions(item, role, uid)]})
    if not workflow.actor_may_move(item, role, uid, to):
        raise ApiError(403, "forbidden", "Only the assignee or a lead can move this item" if to != "closed"
                       else "Only a lead can close or reopen items")
    if to == "in_progress" and item["assignee_id"] is None:
        raise ApiError(409, "assignee_required", "Claim or assign the item before starting work")
    resolution = None
    if workflow.needs_resolution(to):
        resolution = clean_str(body, "resolution", required=True, max_len=5000)
    _check_version(db, user, item, expected)

    now = now_iso()
    sets = {"status": to}
    if to == "awaiting_approval":
        sets.update(resolution=resolution, approval_state="pending", approval_requested_by=uid)
    elif to == "resolved":
        sets.update(resolution=resolution)
    elif to == "open" and item["status"] in ("resolved", "closed"):
        sets.update(resolution=None, approval_state="none", approval_requested_by=None)
    _cas_update(db, user, item, expected, sets, now)

    data = {"from": item["status"], "to": to}
    if resolution:
        data["resolution"] = resolution
    _emit(db, item_id, uid, "approval_requested" if to == "awaiting_approval" else "status_changed", data, now)
    return _result(db, user, item_id)


def _review(db, user, item_id, body, approve):
    item = _fetch(db, user, item_id)
    uid = user["id"]
    expected = _expected(body)
    if item["status"] != "awaiting_approval":
        raise ApiError(409, "invalid_transition", "This item is not awaiting approval")
    authz.require_role(item["my_role"], "lead", "Only a lead can approve or reject")
    if item["approval_requested_by"] == uid:
        raise ApiError(403, "separation_of_duties", "You requested this approval; someone else must review it")
    note = clean_str(body, "note", required=not approve, max_len=2000)
    _check_version(db, user, item, expected)
    now = now_iso()
    if approve:
        _cas_update(db, user, item, expected, {"status": "resolved", "approval_state": "approved"}, now)
    else:
        _cas_update(db, user, item, expected, {"status": "in_progress", "approval_state": "rejected"}, now)
    _emit(db, item_id, uid, "approved" if approve else "rejected",
          {"note": note, "from": "awaiting_approval", "to": "resolved" if approve else "in_progress"}, now)
    return _result(db, user, item_id)


def approve(db, user, item_id, body):
    return _review(db, user, item_id, body, True)


def reject(db, user, item_id, body):
    return _review(db, user, item_id, body, False)


# --- collaboration ---------------------------------------------------------------------------
def comment(db, user, item_id, body):
    item = _fetch(db, user, item_id)
    if not authz.can_comment(item["my_role"]):
        raise ApiError(403, "forbidden", "Viewers cannot comment")
    text = clean_str(body, "body", required=True, max_len=5000)
    now = now_iso()
    # Comments intentionally do NOT bump `version`: someone typing a comment must not invalidate
    # another user's in-progress edit. They do surface the item (updated_at) and count as activity.
    db.execute("UPDATE work_items SET updated_at=?, last_activity_at=? WHERE id=?", (now, now, item_id))
    eid = _emit(db, item_id, user["id"], "comment", {"body": text}, now)
    status, res = _result(db, user, item_id, 201)
    res["event_id"] = eid
    return status, res
