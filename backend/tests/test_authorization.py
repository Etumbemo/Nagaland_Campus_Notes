try:
    from app.authorization import permitted_user_changes
except ModuleNotFoundError:
    from backend.app.authorization import permitted_user_changes


def account(user_id: str, role: str, department_id: str | None = None) -> dict:
    return {"id": user_id, "role": role, "department_id": department_id}


def test_department_admin_cannot_change_own_department():
    admin = account("admin-1", "dept_admin", "department-1")

    assert permitted_user_changes(
        admin, admin, {"department_id": "department-2"}
    ) is None


def test_department_admin_can_update_own_name_email_and_password():
    admin = account("admin-1", "dept_admin", "department-1")
    changes = {"name": "New Name", "email": "new@example.com", "password": "Password!2"}

    assert permitted_user_changes(admin, admin, changes) == changes


def test_department_admin_cannot_update_another_user():
    admin = account("admin-1", "dept_admin", "department-1")
    target = account("admin-2", "dept_admin", "department-1")

    assert permitted_user_changes(admin, target, {"name": "Changed"}) is None


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
