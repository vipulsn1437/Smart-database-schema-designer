import pytest

from app.models.schema_models import CandidateSchema, TableDef, ColumnDef
from app.services.ddl import generate_ddl, DDLGenerationError


def _col(name, dtype="INTEGER", pk=False, fk=None, nullable=True):
    return ColumnDef(name=name, data_type=dtype, nullable=nullable, primary_key=pk, foreign_key=fk)


def _schema(*tables):
    return CandidateSchema(tables=list(tables))


def test_simple_table_with_primary_key_and_not_null():
    ddl = generate_ddl(_schema(
        TableDef(name="students",
                 columns=[_col("student_id", pk=True), _col("name", "VARCHAR(100)", nullable=False)],
                 primary_key=["student_id"]),
    ))
    assert "CREATE TABLE students (" in ddl
    assert "student_id INTEGER," in ddl
    assert "name VARCHAR(100) NOT NULL," in ddl
    assert "PRIMARY KEY (student_id)" in ddl


def test_primary_key_columns_are_not_redundantly_marked_not_null():
    ddl = generate_ddl(_schema(
        TableDef(name="t", columns=[_col("id", pk=True, nullable=False)], primary_key=["id"]),
    ))
    assert "id INTEGER," in ddl and "id INTEGER NOT NULL" not in ddl


def test_composite_primary_key():
    ddl = generate_ddl(_schema(
        TableDef(name="enrollments",
                 columns=[_col("student_id", pk=True), _col("course_id", pk=True)],
                 primary_key=["student_id", "course_id"]),
    ))
    assert "PRIMARY KEY (student_id, course_id)" in ddl


def test_primary_key_falls_back_to_column_flags_when_list_is_empty():
    ddl = generate_ddl(_schema(TableDef(name="t", columns=[_col("id", pk=True)])))
    assert "PRIMARY KEY (id)" in ddl


def test_referenced_table_is_created_before_referencing_table():
    # child listed FIRST in the input; parent must still be emitted first
    ddl = generate_ddl(_schema(
        TableDef(name="sessions",
                 columns=[_col("session_id", pk=True), _col("patient_id", fk="patients.patient_id")],
                 primary_key=["session_id"]),
        TableDef(name="patients", columns=[_col("patient_id", pk=True)], primary_key=["patient_id"]),
    ))
    assert ddl.index("CREATE TABLE patients") < ddl.index("CREATE TABLE sessions")
    assert "CONSTRAINT fk_sessions_patient_id FOREIGN KEY (patient_id) REFERENCES patients (patient_id)" in ddl


def test_self_referencing_foreign_key_is_inline():
    ddl = generate_ddl(_schema(
        TableDef(name="employees",
                 columns=[_col("id", pk=True), _col("manager_id", fk="employees.id")],
                 primary_key=["id"]),
    ))
    assert "REFERENCES employees (id)" in ddl
    assert "ALTER TABLE" not in ddl


def test_circular_references_defer_one_foreign_key_to_alter_table():
    ddl = generate_ddl(_schema(
        TableDef(name="a", columns=[_col("id", pk=True), _col("b_id", fk="b.id")], primary_key=["id"]),
        TableDef(name="b", columns=[_col("id", pk=True), _col("a_id", fk="a.id")], primary_key=["id"]),
    ))
    assert ddl.count("CREATE TABLE") == 2
    assert ddl.count("ALTER TABLE") == 1
    # every CREATE comes before the deferred ALTER
    assert ddl.rindex("CREATE TABLE") < ddl.index("ALTER TABLE")


def test_foreign_key_to_missing_table_is_skipped_with_comment_not_emitted():
    ddl = generate_ddl(_schema(
        TableDef(name="sessions",
                 columns=[_col("session_id", pk=True), _col("patient_id", fk="patients.patient_id")],
                 primary_key=["session_id"]),
    ))
    assert "-- skipped foreign key sessions.patient_id" in ddl
    assert "REFERENCES" not in ddl


def test_foreign_key_to_non_primary_key_column_gets_warning():
    ddl = generate_ddl(_schema(
        TableDef(name="agents",
                 columns=[_col("agent_id", pk=True), _col("agent_name", "VARCHAR(100)")],
                 primary_key=["agent_id"]),
        TableDef(name="listings",
                 columns=[_col("property_id", pk=True), _col("agent_name", "VARCHAR(100)", fk="agents.agent_name")],
                 primary_key=["property_id"]),
    ))
    assert "-- WARNING: listings.agent_name references agents.agent_name" in ddl


def test_non_postgres_types_are_mapped():
    ddl = generate_ddl(_schema(
        TableDef(name="t",
                 columns=[_col("id", pk=True), _col("created", "DATETIME"), _col("flag", "TINYINT(1)"),
                          _col("ratio", "DOUBLE"), _col("note", "STRING")],
                 primary_key=["id"]),
    ))
    assert "created TIMESTAMP" in ddl
    assert "flag SMALLINT" in ddl and "TINYINT" not in ddl
    assert "ratio DOUBLE PRECISION" in ddl
    assert "note TEXT" in ddl


def test_numeric_precision_is_preserved():
    ddl = generate_ddl(_schema(
        TableDef(name="t", columns=[_col("id", pk=True), _col("price", "decimal( 10 , 2 )")], primary_key=["id"]),
    ))
    assert "price DECIMAL(10,2)" in ddl


def test_reserved_word_identifiers_are_quoted_plain_ones_are_not():
    ddl = generate_ddl(_schema(
        TableDef(name="order", columns=[_col("id", pk=True), _col("user", "VARCHAR(50)")], primary_key=["id"]),
    ))
    assert 'CREATE TABLE "order" (' in ddl
    assert '"user" VARCHAR(50)' in ddl
    assert "    id INTEGER," in ddl  # not quoted


def test_identifier_with_quote_character_is_escaped():
    ddl = generate_ddl(_schema(
        TableDef(name='we"ird', columns=[_col("id", pk=True)], primary_key=["id"]),
    ))
    assert 'CREATE TABLE "we""ird" (' in ddl


# ---- security: schema content originates from an LLM ----

@pytest.mark.parametrize("bad_type", [
    "INTEGER); DROP TABLE users; --",
    "VARCHAR(100) DEFAULT (SELECT 1)",
    "INT; DELETE FROM x",
    "",
    "VARCHAR(abc)",
])
def test_malicious_or_malformed_data_type_is_rejected(bad_type):
    with pytest.raises(DDLGenerationError):
        generate_ddl(_schema(
            TableDef(name="t", columns=[_col("id", pk=True), _col("x", bad_type)], primary_key=["id"]),
        ))


def test_sql_injection_in_table_name_is_neutralised_by_quoting():
    ddl = generate_ddl(_schema(
        TableDef(name='x"; DROP TABLE users; --', columns=[_col("id", pk=True)], primary_key=["id"]),
    ))
    # the whole hostile string must sit inside ONE quoted identifier
    assert 'CREATE TABLE "x""; DROP TABLE users; --" (' in ddl


def test_overlong_identifier_is_rejected():
    with pytest.raises(DDLGenerationError):
        generate_ddl(_schema(TableDef(name="a" * 64, columns=[_col("id", pk=True)], primary_key=["id"])))


# ---- structural problems DDL cannot paper over ----

def test_empty_schema_is_rejected():
    with pytest.raises(DDLGenerationError):
        generate_ddl(CandidateSchema(tables=[]))


def test_duplicate_table_names_are_rejected():
    t = TableDef(name="t", columns=[_col("id", pk=True)], primary_key=["id"])
    with pytest.raises(DDLGenerationError):
        generate_ddl(_schema(t, t))


def test_duplicate_column_names_are_rejected():
    with pytest.raises(DDLGenerationError):
        generate_ddl(_schema(TableDef(name="t", columns=[_col("id"), _col("id")], primary_key=[])))


def test_primary_key_naming_a_missing_column_is_rejected():
    with pytest.raises(DDLGenerationError):
        generate_ddl(_schema(TableDef(name="t", columns=[_col("id")], primary_key=["nope"])))
