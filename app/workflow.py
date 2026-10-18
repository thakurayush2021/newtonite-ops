"""Work-item state machine. Pure functions: no I/O, trivially testable.

            +--------------------------------------+
            v                                      |
   open --> in_progress --> resolved --> closed    |
    |  ^        |  ^            |          |       |
    |  +-- blocked <-+          +--reopen--+       |
    |                                              |
    +-> closed       in_progress --(needs approval)--> awaiting_approval --approve--> resolved
                                                          |
                                                          +--reject--> in_progress
"""
from . import authz

STATUSES = ("open", "in_progress", "blocked", "awaiting_approval", "resolved", "closed")
PRIORITIES = ("low", "normal", "high", "urgent")
PRANK = {p: i for i, p in enumerate(PRIORITIES)}
ACTIVE = ("open", "in_progress", "blocked", "awaiting_approval")

_GRAPH = {
    "open": ("in_progress", "blocked", "closed"),
    "in_progress": ("open", "blocked", "resolved", "awaiting_approval"),
    "blocked": ("open", "in_progress", "closed"),
    "awaiting_approval": (),                      # only approve/reject leave this state
    "resolved": ("open", "closed"),
    "closed": ("open",),
}


def graph_targets(item):
    """Structurally legal next states, taking the approval requirement into account."""
    out = []
    for to in _GRAPH[item["status"]]:
        if to == "resolved" and item["requires_approval"]:
            continue                              # cannot skip approval
        if to == "awaiting_approval" and not item["requires_approval"]:
            continue
        out.append(to)
    return out


def actor_may_move(item, role, uid, to):
    """May this actor perform this transition (ignoring data preconditions)?"""
    if not authz.at_least(role, "member"):
        return False
    if authz.at_least(role, "lead"):
        return True
    if to == "closed":
        return False                              # closing/cancelling is a lead decision
    if item["status"] == "closed":
        return False                              # reopening closed work is a lead decision
    return item["assignee_id"] == uid


def needs_resolution(to):
    return to in ("resolved", "awaiting_approval")


def allowed_transitions(item, role, uid):
    """What the UI may offer. Includes preconditions that are knowable up-front."""
    out = []
    for to in graph_targets(item):
        if not actor_may_move(item, role, uid, to):
            continue
        if to == "in_progress" and item["assignee_id"] is None:
            continue                              # must be claimed/assigned first
        out.append({"to": to, "needs_resolution": needs_resolution(to)})
    return out
