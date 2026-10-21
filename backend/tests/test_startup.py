from collections.abc import Callable
from typing import Any

import pytest

try:
    from app import main
    from app.database import verify_schema
    from app.dev_seed import seed_demo_data
except ModuleNotFoundError:
    from backend.app import main
    from backend.app.database import verify_schema
    from backend.app.dev_seed import seed_demo_data


class FakeCursor:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.result: dict[str, Any] | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        sql = sql.strip()
        if sql.startswith("SELECT id FROM departments"):
            self.result = self.state["departments"].get(params[0])
        elif sql.startswith("INSERT INTO departments"):
            department_id, name, code = params
            self.state["departments"][code] = {"id": department_id, "name": name}
            self.state["inserts"] += 1
            self.result = None
        elif sql.startswith("SELECT id FROM papers"):
            self.result = self.state["papers"].get(params[0])
            if self.result is None:
                self.result = next(
                    (
                        paper
                        for paper in self.state["papers"].values()
                        if paper.get("subject_code") == params[1]
                    ),
                    None,
                )
        elif sql.startswith("INSERT INTO papers"):
            self.state["papers"][params[0]] = {
                "id": params[0],
                "subject_code": params[2],
            }
            self.state["inserts"] += 1
            self.result = None
        else:
            raise AssertionError(f"Unexpected SQL in fake seed database: {sql}")

    def fetchone(self):
        return self.result


class FakeConnection:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state = state or {"departments": {}, "papers": {}, "inserts": 0}
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self, *_args):
        return FakeCursor(self.state)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class MissingSchemaCursor:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql: str, _params: tuple = ()) -> None:
        self.sql = sql

    def fetchall(self):
        return []


class MissingSchemaConnection(FakeConnection):
    def cursor(self, *_args):
        return MissingSchemaCursor()


@pytest.fixture
def startup_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SCHEMA_AUTO_MIGRATE",
        "SEED_DEMO_DATA",
        "ADMIN_EMAIL",
        "ADMIN_PASSWORD",
        "ADMIN_NAME",
    ):
        monkeypatch.delenv(name, raising=False)


def configure_startup(
    monkeypatch: pytest.MonkeyPatch,
    connection_factory: Callable[[], FakeConnection],
) -> dict[str, list[str]]:
    calls = {"ddl": [], "verified": [], "seed": [], "admin_queries": []}
    monkeypatch.setattr(main, "connect_mysql", connection_factory)
    monkeypatch.setattr(main, "verify_schema", lambda _connection: calls["verified"].append("yes"))
    monkeypatch.setattr(main, "ensure_database_exists", lambda: calls["ddl"].append("database"))
    monkeypatch.setattr(main, "create_tables", lambda: calls["ddl"].append("tables"))
    monkeypatch.setattr(
        main,
        "seed_demo_data",
        lambda _connection: calls["seed"].append("seeded") or 0,
    )
    monkeypatch.setattr(
        main,
        "query_one",
        lambda *_args: calls["admin_queries"].append("queried") or None,
    )
    return calls


def test_startup_with_flags_off_performs_no_demo_inserts_or_ddl(
    startup_environment, monkeypatch: pytest.MonkeyPatch
):
    connection = FakeConnection()
    calls = configure_startup(monkeypatch, lambda: connection)

    main.initialize_database()

    assert calls["ddl"] == []
    assert calls["seed"] == []
    assert calls["verified"] == ["yes"]
    assert calls["admin_queries"] == []
    assert connection.state["inserts"] == 0
    assert connection.closed


def test_startup_with_demo_flag_seeds_once_and_is_idempotent(
    startup_environment, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SEED_DEMO_DATA", "true")
    state = {"departments": {}, "papers": {}, "inserts": 0}
    connections = [FakeConnection(state), FakeConnection(state)]
    connection_iterator = iter(connections)
    calls = configure_startup(monkeypatch, lambda: next(connection_iterator))
    monkeypatch.setattr(main, "seed_demo_data", seed_demo_data)

    main.initialize_database()
    first_insert_count = state["inserts"]
    main.initialize_database()

    assert first_insert_count == 8
    assert state["inserts"] == first_insert_count
    assert len(state["departments"]) == 3
    assert len(state["papers"]) == 5
    assert calls["ddl"] == []
    assert calls["seed"] == []
    assert all(connection.closed for connection in connections)


def test_startup_admin_bootstrap_is_skipped_without_environment_values(
    startup_environment, monkeypatch: pytest.MonkeyPatch
):
    connection = FakeConnection()
    calls = configure_startup(monkeypatch, lambda: connection)

    main.initialize_database()

    assert calls["admin_queries"] == []
    assert connection.closed


@pytest.mark.parametrize(
    ("admin_email", "admin_password"),
    [("admin@example.com", ""), ("", "secret")],
)
def test_startup_requires_both_admin_environment_values(
    startup_environment,
    monkeypatch: pytest.MonkeyPatch,
    admin_email: str,
    admin_password: str,
):
    monkeypatch.setenv("ADMIN_EMAIL", admin_email)
    monkeypatch.setenv("ADMIN_PASSWORD", admin_password)
    connection_calls: list[str] = []
    monkeypatch.setattr(main, "connect_mysql", lambda: connection_calls.append("connect"))

    with pytest.raises(
        RuntimeError,
        match="Configure both ADMIN_EMAIL and ADMIN_PASSWORD, or leave both empty",
    ):
        main.initialize_database()

    assert connection_calls == []


def test_missing_schema_without_migration_opt_in_fails_actionably(
    startup_environment, monkeypatch: pytest.MonkeyPatch
):
    connection = MissingSchemaConnection()
    configure_startup(monkeypatch, lambda: connection)
    monkeypatch.setattr(main, "verify_schema", verify_schema)

    with pytest.raises(RuntimeError, match="SCHEMA_AUTO_MIGRATE=true"):
        main.initialize_database()

    assert connection.rollbacks == 1
    assert connection.closed


def test_startup_migration_flag_runs_schema_setup(
    startup_environment, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SCHEMA_AUTO_MIGRATE", "true")
    connection = FakeConnection()
    calls = configure_startup(monkeypatch, lambda: connection)

    main.initialize_database()

    assert calls["ddl"] == ["database", "tables"]
    assert connection.closed
