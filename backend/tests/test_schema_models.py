from app.models.schema_models import CandidateSchema, TableDef, ColumnDef, FunctionalDependency


def test_minimal_schema_parses():
    schema = CandidateSchema(
        tables=[
            TableDef(
                name="students",
                columns=[
                    ColumnDef(name="student_id", data_type="INTEGER", primary_key=True, nullable=False),
                    ColumnDef(name="student_name", data_type="VARCHAR(100)", nullable=False),
                ],
                primary_key=["student_id"],
            )
        ],
        functional_dependencies=[
            FunctionalDependency(table="students", determinant=["student_id"], dependent=["student_name"])
        ],
    )
    assert len(schema.tables) == 1
    assert schema.tables[0].primary_key == ["student_id"]
    assert schema.functional_dependencies[0].determinant == ["student_id"]


def test_composite_primary_key_supported():
    table = TableDef(
        name="enrollments",
        columns=[
            ColumnDef(name="student_id", data_type="INTEGER", foreign_key="students.student_id"),
            ColumnDef(name="course_id", data_type="INTEGER", foreign_key="courses.course_id"),
            ColumnDef(name="grade", data_type="VARCHAR(2)"),
        ],
        primary_key=["student_id", "course_id"],
    )
    assert table.primary_key == ["student_id", "course_id"]


def test_defaults_are_empty_not_none():
    schema = CandidateSchema(tables=[])
    assert schema.functional_dependencies == []
    assert schema.structural_issues == []
    assert schema.normalization_violations == []
    assert schema.correction_attempt == 0
