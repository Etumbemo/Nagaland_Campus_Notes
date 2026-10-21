from collections.abc import Mapping
from typing import Any


ROLE_UPDATE_FIELDS = {
    "user": frozenset({"name", "email", "password"}),
    "institute": frozenset({"name", "email", "institute_name", "password"}),
}


def permitted_user_changes(
    requester: Mapping[str, Any],
    target: Mapping[str, Any],
    changes: Mapping[str, Any],
) -> dict[str, Any] | None:
    if requester.get("role") == "super_admin":
        return dict(changes)

    if (
        requester.get("id") != target.get("id")
        or target.get("department_id") != requester.get("department_id")
    ):
        return None

    allowed_fields = ROLE_UPDATE_FIELDS.get(requester.get("role"))
    if allowed_fields is None or not set(changes).issubset(allowed_fields):
        return None
    return dict(changes)
