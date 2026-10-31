"""
Tests for live deployment.

Two groups:

  * Unit tests, always run: input validation, the HTTP layer (with a fake
    deployer), and failure handling. No database needed.

  * Integration tests, run only when TEST_DATABASE_URL points at a real
    PostgreSQL server (they create and remove their own sandbox schemas):

        $env:TEST_DATABASE_URL = "postgresql://postgres:devpass@127.0.0.1:5432/postgres"
        python -m pytest tests/test_deploy.py -v
"""

import os

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api import routes
from app.api.routes import get_deployer
from app.main import app
from app.models.schema_models import CandidateSchema, ColumnDef, DeployResponse, FunctionalDependency, TableDef
from app.services.ddl import generate_ddl
from app.services.deploy import DeploymentUnavailable, InvalidSandboxName, SandboxDeployer


def _col(name, dtype="INTEGER", pk=False, fk=None, nullable=True):
    return ColumnDef(name=name, data_type=dtype, nullable=nullable, primary_key=pk, foreign_key=fk)


def _fd(table, det, dep):
    return FunctionalDependency(table=table, determinant=det, dependent=dep)


def clinic_schema():
    """Normalized, two tables with a foreign key and a DATETIME column."""
    return CandidateSchema(
        tables=[
            TableDef(name="branches", columns=[_col("branch_id", pk=True, nullable=False)], primary_key=["branch_id"]),
            TableDef(name="therapists",
                     columns=[_col("therapist_id", pk=True, nullable=False),
                              _col("branch_id", fk="branches.branch_id", nullable=False),
                              _col("hired_at", "DATETIME")],
                     primary_key=["therapist_id"]),
        ],
        functional_dependencies=[
            _fd("therapists", ["therapist_id"], ["branch_id", "hired_at"]),
        ],
    )


def violating_schema():
    return CandidateSchema(
        tables=[TableDef(name="listings",
                         columns=[_col("property_id", pk=True), _col("agent_name", "VARCHAR(100)"),
                                  _col("branch_office", "VARCHAR(100)")],
                         primary_key=["property_id"])],
        functional_dependencies=[
            _fd("listings", ["property_id"], ["agent_name", "branch_office"]),
            _fd("listings", ["agent_name"], ["branch_office"]),
        ],
    )


# =====================================================================
# Unit tests (no database)
# =====================================================================

@pytest.mark.parametrize("bad_name", [
    "public",
    "sandbox_",
    "sandbox_xyz",
    "sandbox_0123456789abc",           # 13 hex chars
    "SANDBOX_0123456789ab",            # wrong case
    "sandbox_0123456789ab; DROP SCHEMA public CASCADE",
    'sandbox_0123456789ab"',
    "",
])
def test_teardown_rejects_names_this_feature_never_creates(bad_name):
    # Rejected BEFORE any connection is attempted, so a bogus DSN is fine.
    with pytest.raises(InvalidSandboxName):
        SandboxDeployer("postgresql://nobody@127.0.0.1:1/none").teardown(bad_name)


def test_unreachable_database_raises_unavailable_without_leaking_the_password():
    deployer = SandboxDeployer("postgresql://someuser:SuperSecretPw123@127.0.0.1:1/somedb")
    with pytest.raises(DeploymentUnavailable) as exc:
        deployer.deploy("CREATE TABLE t (id INTEGER);")
    assert "SuperSecretPw123" not in str(exc.value)
    assert "DATABASE_URL" in str(exc.value)


class FakeDeployer:
    def __init__(self, response=None, raises=None, teardown_result=True):
        self.response, self.raises, self.teardown_result = response, raises, teardown_result
        self.deploy_calls, self.teardown_calls = [], []

    def deploy(self, ddl, keep=False):
        self.deploy_calls.append((ddl, keep))
        if self.raises:
            raise self.raises
        return self.response or DeployResponse(deployed=True, kept=keep, ddl=ddl)

    def teardown(self, name):
        self.teardown_calls.append(name)
        if not name.startswith("sandbox_"):
            raise InvalidSandboxName("bad name")
        return self.teardown_result


@pytest.fixture
def client():
    yield TestClient(app)
    app.dependency_overrides.clear()


def use_deployer(d):
    app.dependency_overrides[get_deployer] = lambda: d


def test_deploy_returns_503_with_instructions_when_database_url_not_configured(client, monkeypatch):
    monkeypatch.setattr(routes.settings, "database_url", "")
    r = client.post("/api/deploy", json=clinic_schema().model_dump())
    assert r.status_code == 503
    assert "DATABASE_URL" in r.json()["detail"]


def test_other_endpoints_still_work_without_a_database(client, monkeypatch):
    monkeypatch.setattr(routes.settings, "database_url", "")
    assert client.post("/api/validate", json=clinic_schema().model_dump()).status_code == 200
    assert client.post("/api/ddl", json=clinic_schema().model_dump()).status_code == 200


def test_deploy_refuses_a_schema_with_validation_issues_and_never_touches_the_database(client):
    fake = FakeDeployer()
    use_deployer(fake)
    r = client.post("/api/deploy", json=violating_schema().model_dump())
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert any("3NF violation" in i for i in detail["normalization_issues"])
    assert fake.deploy_calls == []


def test_deploy_rejects_unsafe_column_type_with_422_before_touching_the_database(client):
    bad = CandidateSchema(
        tables=[TableDef(name="t", columns=[_col("id", pk=True), _col("x", "INTEGER); DROP TABLE users; --")],
                         primary_key=["id"])],
        functional_dependencies=[_fd("t", ["id"], ["x"])],
    )
    fake = FakeDeployer()
    use_deployer(fake)
    assert client.post("/api/deploy", json=bad.model_dump()).status_code == 422
    assert fake.deploy_calls == []


def test_deploy_passes_generated_ddl_and_keep_flag_to_the_deployer(client):
    fake = FakeDeployer()
    use_deployer(fake)
    r = client.post("/api/deploy?keep=true", json=clinic_schema().model_dump())
    assert r.status_code == 200
    ddl, keep = fake.deploy_calls[0]
    assert ddl == generate_ddl(clinic_schema())
    assert keep is True
    # default is a dry run
    client.post("/api/deploy", json=clinic_schema().model_dump())
    assert fake.deploy_calls[1][1] is False


def test_deploy_reports_a_database_rejection_in_the_body(client):
    use_deployer(FakeDeployer(DeployResponse(deployed=False, ddl="...", error="no unique constraint matching given keys")))
    body = client.post("/api/deploy", json=clinic_schema().model_dump()).json()
    assert body["deployed"] is False and "no unique constraint" in body["error"]


def test_deploy_maps_unreachable_database_to_503(client):
    use_deployer(FakeDeployer(raises=DeploymentUnavailable("Could not connect")))
    assert client.post("/api/deploy", json=clinic_schema().model_dump()).status_code == 503


def test_delete_rejects_non_sandbox_names_with_400(client):
    use_deployer(FakeDeployer())
    assert client.delete("/api/deploy/public").status_code == 400


def test_delete_reports_whether_anything_was_dropped(client):
    use_deployer(FakeDeployer(teardown_result=True))
    assert client.delete("/api/deploy/sandbox_0123456789ab").json() == {"dropped": True}
    use_deployer(FakeDeployer(teardown_result=False))
    assert client.delete("/api/deploy/sandbox_0123456789ab").json() == {"dropped": False}


# =====================================================================
# Integration tests (real PostgreSQL; opt-in via TEST_DATABASE_URL)
# =====================================================================

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to run tests against a real PostgreSQL")


def _sandbox_schemas():
    with psycopg.connect(DSN, connect_timeout=5) as conn:
        rows = conn.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'sandbox\\_%'").fetchall()
    return {r[0] for r in rows}


@needs_db
def test_dry_run_reports_real_tables_and_leaves_nothing_behind():
    before = _sandbox_schemas()
    result = SandboxDeployer(DSN).deploy(generate_ddl(clinic_schema()), keep=False)

    assert result.deployed is True and result.kept is False and result.schema_name is None
    by_name = {t.name: t for t in result.tables}
    assert set(by_name) == {"branches", "therapists"}
    cols = {c.name: c for c in by_name["therapists"].columns}
    assert cols["hired_at"].type == "timestamp without time zone"   # DATETIME was mapped
    assert cols["branch_id"].nullable is False
    assert any(c.startswith("PRIMARY KEY") for c in by_name["therapists"].constraints)
    assert any("FOREIGN KEY (branch_id) REFERENCES" in c for c in by_name["therapists"].constraints)
    assert _sandbox_schemas() == before            # nothing left behind


@needs_db
def test_keep_commits_then_teardown_removes_it():
    deployer = SandboxDeployer(DSN)
    result = deployer.deploy(generate_ddl(clinic_schema()), keep=True)
    assert result.deployed and result.kept and result.schema_name
    assert result.schema_name in _sandbox_schemas()

    assert deployer.teardown(result.schema_name) is True
    assert result.schema_name not in _sandbox_schemas()
    assert deployer.teardown(result.schema_name) is False   # already gone


@needs_db
def test_database_rejection_is_reported_with_postgres_message_and_rolls_back_cleanly():
    before = _sandbox_schemas()
    # FK to a column that is not unique: structurally valid, but PostgreSQL refuses it.
    schema = CandidateSchema(
        tables=[
            TableDef(name="agents", columns=[_col("agent_id", pk=True), _col("agent_name", "VARCHAR(100)")],
                     primary_key=["agent_id"]),
            TableDef(name="listings",
                     columns=[_col("property_id", pk=True), _col("agent_name", "VARCHAR(100)", fk="agents.agent_name")],
                     primary_key=["property_id"]),
        ]
    )
    result = SandboxDeployer(DSN).deploy(generate_ddl(schema), keep=True)
    assert result.deployed is False
    assert "unique constraint" in result.error
    assert result.tables == [] and result.schema_name is None
    assert _sandbox_schemas() == before            # even keep=True leaves nothing after a failure


@needs_db
def test_deployment_cannot_touch_tables_in_public():
    marker = "zz_sandbox_marker_tbl"
    with psycopg.connect(DSN, connect_timeout=5, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS public.{marker}")
        conn.execute(f"CREATE TABLE public.{marker} (original_col INTEGER)")
    try:
        schema = CandidateSchema(
            tables=[TableDef(name=marker, columns=[_col("id", pk=True), _col("other", "TEXT")], primary_key=["id"])]
        )
        result = SandboxDeployer(DSN).deploy(generate_ddl(schema), keep=False)
        assert result.deployed is True          # same table name works: it lands in the sandbox schema
        with psycopg.connect(DSN, connect_timeout=5) as conn:
            cols = [r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s",
                (marker,)).fetchall()]
        assert cols == ["original_col"]         # the real public table is untouched
    finally:
        with psycopg.connect(DSN, connect_timeout=5, autocommit=True) as conn:
            conn.execute(f"DROP TABLE IF EXISTS public.{marker}")


@needs_db
def test_circular_references_deploy_via_deferred_alter_table():
    schema = CandidateSchema(tables=[
        TableDef(name="a", columns=[_col("id", pk=True), _col("b_id", fk="b.id")], primary_key=["id"]),
        TableDef(name="b", columns=[_col("id", pk=True), _col("a_id", fk="a.id")], primary_key=["id"]),
    ])
    result = SandboxDeployer(DSN).deploy(generate_ddl(schema), keep=False)
    assert result.deployed is True, result.error
    assert {t.name for t in result.tables} == {"a", "b"}
