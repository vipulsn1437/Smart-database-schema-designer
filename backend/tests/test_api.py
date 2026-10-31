"""
Tests for the HTTP API. A FakeGenerator is injected through FastAPI's
dependency override, so these run with no API key and no network, and
exercise the real routing, validation, correction-loop and DDL code.
"""

import pytest
from fastapi.testclient import TestClient

from app.api.routes import get_generator
from app.main import app
from app.models.schema_models import CandidateSchema, ColumnDef, FunctionalDependency, TableDef
from app.services.ai_generation import SchemaGenerationError


def _col(name, dtype="INTEGER", pk=False, fk=None, nullable=True):
    return ColumnDef(name=name, data_type=dtype, nullable=nullable, primary_key=pk, foreign_key=fk)


def _fd(table, det, dep):
    return FunctionalDependency(table=table, determinant=det, dependent=dep)


def clean_schema():
    return CandidateSchema(
        tables=[TableDef(name="students", columns=[_col("student_id", pk=True), _col("name", "VARCHAR(100)")],
                         primary_key=["student_id"])],
        functional_dependencies=[_fd("students", ["student_id"], ["name"])],
    )


def violating_schema():
    """3NF/BCNF violation: agent_name -> branch_office with PK property_id."""
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


def unsafe_type_schema():
    """Passes both validators but carries a hostile column type."""
    return CandidateSchema(
        tables=[TableDef(name="t", columns=[_col("id", pk=True), _col("x", "INTEGER); DROP TABLE users; --")],
                         primary_key=["id"])],
        functional_dependencies=[_fd("t", ["id"], ["x"])],
    )


class FakeGenerator:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def _next(self):
        r = self._responses[self.calls]
        self.calls += 1
        if r == "FAIL":
            raise SchemaGenerationError("simulated unparsable output")
        return r, f"raw_{self.calls - 1}"

    def generate(self, requirements):
        return self._next()

    def generate_correction(self, requirements, previous_schema_json, issues):
        return self._next()


@pytest.fixture
def client():
    yield TestClient(app)
    app.dependency_overrides.clear()


def use_generator(gen):
    app.dependency_overrides[get_generator] = lambda: gen


REQ = {"requirements": "a system to track students and their courses"}


# ---------------------------------------------------------------- /health

def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


# ---------------------------------------------------------------- /api/validate

def test_validate_clean_schema(client):
    r = client.post("/api/validate", json=clean_schema().model_dump())
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["structural_issues"] == [] and body["normalization_issues"] == []


def test_validate_reports_normalization_violation(client):
    r = client.post("/api/validate", json=violating_schema().model_dump())
    body = r.json()
    assert body["valid"] is False
    assert any("3NF violation" in i for i in body["normalization_issues"])


def test_validate_reports_structural_issue(client):
    schema = CandidateSchema(tables=[TableDef(name="t", columns=[_col("id")], primary_key=[])])
    body = client.post("/api/validate", json=schema.model_dump()).json()
    assert body["valid"] is False
    assert body["structural_issues"]  # missing primary key


def test_validate_rejects_malformed_body(client):
    assert client.post("/api/validate", json={"nope": 1}).status_code == 422


# ---------------------------------------------------------------- /api/ddl

def test_ddl_export(client):
    r = client.post("/api/ddl", json=clean_schema().model_dump())
    assert r.status_code == 200
    assert "CREATE TABLE students (" in r.json()["ddl"]


def test_ddl_export_rejects_unsafe_schema_with_422(client):
    r = client.post("/api/ddl", json=unsafe_type_schema().model_dump())
    assert r.status_code == 422
    assert "unsupported data type" in r.json()["detail"]


# ---------------------------------------------------------------- /api/generate

def test_generate_returns_schema_without_validating(client):
    use_generator(FakeGenerator([violating_schema()]))
    r = client.post("/api/generate", json=REQ)
    assert r.status_code == 200
    body = r.json()
    assert body["schema"]["tables"][0]["name"] == "listings"
    assert body["schema"]["normalization_violations"] == []  # generate does not validate


def test_generate_maps_unparsable_llm_output_to_502(client):
    use_generator(FakeGenerator(["FAIL"]))
    assert client.post("/api/generate", json=REQ).status_code == 502


def test_generate_rejects_too_short_requirements(client):
    use_generator(FakeGenerator([clean_schema()]))
    assert client.post("/api/generate", json={"requirements": "hi"}).status_code == 422


# ---------------------------------------------------------------- /api/design

def test_design_clean_first_attempt(client):
    use_generator(FakeGenerator([clean_schema()]))
    body = client.post("/api/design", json=REQ).json()
    assert body["converged"] is True
    assert body["baseline_issue_count"] == 0
    assert body["correction_rounds_used"] == 0
    assert len(body["attempts"]) == 1
    assert "CREATE TABLE students" in body["ddl"]


def test_design_corrects_violation_then_returns_ddl(client):
    use_generator(FakeGenerator([violating_schema(), clean_schema()]))
    body = client.post("/api/design", json=REQ).json()
    assert body["converged"] is True
    assert body["baseline_issue_count"] > 0 and body["final_issue_count"] == 0
    assert body["correction_rounds_used"] == 1
    assert [a["attempt_number"] for a in body["attempts"]] == [0, 1]
    assert body["attempts"][0]["normalization_issues"]  # the violation is reported
    assert body["ddl"] is not None


def test_design_non_convergence_is_reported_honestly_with_no_ddl(client):
    use_generator(FakeGenerator([violating_schema()] * 4))
    body = client.post("/api/design", json=REQ).json()
    assert body["converged"] is False
    assert body["final_issue_count"] > 0
    assert body["correction_rounds_used"] == 3
    assert len(body["attempts"]) == 4
    assert body["ddl"] is None  # never offer SQL for a schema that still has issues


def test_design_failed_correction_call_is_reported_not_crashed(client):
    use_generator(FakeGenerator([violating_schema(), "FAIL"]))
    r = client.post("/api/design", json=REQ)
    assert r.status_code == 200
    body = r.json()
    assert body["converged"] is False
    assert body["attempts"][-1]["generation_failed"] is True


def test_design_initial_generation_failure_maps_to_502(client):
    use_generator(FakeGenerator(["FAIL"]))
    assert client.post("/api/design", json=REQ).status_code == 502


def test_design_schema_that_passes_validators_but_has_unsafe_ddl_is_not_converged(client):
    use_generator(FakeGenerator([unsafe_type_schema()]))
    body = client.post("/api/design", json=REQ).json()
    assert body["converged"] is False
    assert body["ddl"] is None
    assert "unsupported data type" in body["ddl_error"]
