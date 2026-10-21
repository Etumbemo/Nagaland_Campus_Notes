try:
    from app.authorization import permitted_user_changes
except ModuleNotFoundError:
    from backend.app.authorization import permitted_user_changes


def account(user_id: str, role: str, department_id: str | None = None) -> dict:
    return {"id": user_id, "role": role, "department_id": department_id}


def test_legacy_department_admin_cannot_update_account():
    legacy_admin = account("admin-1", "dept_admin", "department-1")

    assert permitted_user_changes(
        legacy_admin, legacy_admin, {"name": "Changed"}
    ) is None


def test_super_admin_may_set_department_id():
    admin = account("root", "super_admin")
    target = account("user-1", "user")
    changes = {"department_id": "department-2"}

    assert permitted_user_changes(admin, target, changes) == changes


def test_reader_and_institute_cannot_set_forbidden_fields():
    for role in ("user", "institute"):
        requester = account("user-1", role)
        target = account("user-1", role)
        for changes in (
            {"department_id": "department-2"},
            {"role": "super_admin"},
            {"username": "not-allowed"},
        ):
            assert permitted_user_changes(requester, target, changes) is None
