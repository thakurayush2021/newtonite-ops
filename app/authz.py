"""Authorization: ONE set of predicates used both to enforce actions and to tell the UI what to show.

Model: per-team roles. A user's power over a work item is decided by their role in the item's team
and (for some actions) their relationship to the item (assignee / creator).

    viewer  read only
    member  create, comment, claim; edit items they created or hold; move items they hold
    lead    everything a member can + assign others, set priority, move any item, approve/reject
    admin   lead + manage team membership

Non-members get 404 (not 403) on items so item existence does not leak across teams.
"""
from .common import ApiError

RANK = {"viewer": 0, "member": 1, "lead": 2, "admin": 3}


def at_least(role, minimum):
    return role is not None and RANK[role] >= RANK[minimum]


def require_role(role, minimum, message="You do not have permission to do that"):
    if not at_least(role, minimum):
        raise ApiError(403, "forbidden", message, {"required_role": minimum})


def is_closed(item):
    return item["status"] == "closed"


def can_comment(role):
    return at_least(role, "member")


def can_claim(item, role):
    return at_least(role, "member") and item["assignee_id"] is None and not is_closed(item)


def can_release(item, role, uid):
    if item["assignee_id"] is None or is_closed(item):
        return False
    return at_least(role, "lead") or (at_least(role, "member") and item["assignee_id"] == uid)


def can_assign(item, role):
    return at_least(role, "lead") and not is_closed(item)


def can_edit(item, role, uid):
    if is_closed(item):
        return False
    return at_least(role, "lead") or (
        at_least(role, "member") and uid in (item["creator_id"], item["assignee_id"])
    )


def can_set_priority(item, role):
    return at_least(role, "lead") and not is_closed(item)


def can_review(item, role, uid):
    """Approve/reject: lead+, and never the person who asked for the approval (separation of duties)."""
    return (
        item["status"] == "awaiting_approval"
        and at_least(role, "lead")
        and item["approval_requested_by"] != uid
    )
