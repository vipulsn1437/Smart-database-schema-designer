from app.models.schema_models import CandidateSchema, TableDef, ColumnDef
from app.services.structural import validate_structure


def _col(name, dtype="INTEGER", pk=False, fk=None, nullable=True):
    return ColumnDef(name=name, data_type=dtype, nullable=nullable, primary_key=pk, foreign_key=fk)


def test_clean_schema_has_no_structural_issues():
    schema = CandidateSchema(
        tables=[
            TableDef(
                name="students",
                columns=[_col("student_id", pk=True, nullable=False), _col("name", "VARCHAR(100)")],
                primary_key=["student_id"],
            ),
            TableDef(
                name="enrollments",
                columns=[
                    _col("student_id", fk="students.student_id", nullable=False),
                    _col("course_id", fk="courses.course_id", nullable=False),
                ],
                primary_key=["student_id", "course_id"],
            ),
            TableDef(
                name="courses",
                columns=[_col("course_id", pk=True, nullable=False), _col("title", "VARCHAR(200)")],
                primary_key=["course_id"],
            ),
        ]
    )
    assert validate_structure(schema) == []


def test_duplicate_table_name_detected():
    t = TableDef(name="students", columns=[_col("id", pk=True)], primary_key=["id"])
    schema = CandidateSchema(tables=[t, t])
    issues = validate_structure(schema)
    assert any("Duplicate table name" in i for i in issues)


def test_duplicate_column_name_detected():
    schema = CandidateSchema(
        tables=[
            TableDef(
                name="students",
                columns=[_col("id", pk=True), _col("id", "VARCHAR(50)")],
                primary_key=["id"],
            )
        ]
    )
    issues = validate_structure(schema)
    assert any("Duplicate column name" in i for i in issues)


def test_missing_primary_key_detected():
    schema = CandidateSchema(
        tables=[TableDef(name="students", columns=[_col("id"), _col("name", "VARCHAR(100)")], primary_key=[])]
    )
    issues = validate_structure(schema)
    assert any("no declared primary key" in i for i in issues)


def test_orphaned_foreign_key_detected():
    schema = CandidateSchema(
        tables=[
            TableDef(
                name="orders",
                columns=[_col("order_id", pk=True), _col("customer_id", fk="customers.customer_id")],
                primary_key=["order_id"],
            )
            # Note: 'customers' table does not exist at all
        ]
    )
    issues = validate_structure(schema)
    assert any("does not exist in the schema" in i for i in issues)


def test_orphaned_foreign_key_column_detected():
    schema = CandidateSchema(
        tables=[
            TableDef(
                name="orders",
                columns=[_col("order_id", pk=True), _col("customer_id", fk="customers.missing_col")],
                primary_key=["order_id"],
            ),
            TableDef(name="customers", columns=[_col("customer_id", pk=True)], primary_key=["customer_id"]),
        ]
    )
    issues = validate_structure(schema)
    assert any("which does not exist" in i for i in issues)
