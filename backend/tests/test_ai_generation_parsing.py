import pytest
from app.services.ai_generation import _extract_json, _parse_schema, SchemaGenerationError

SAMPLE_SCHEMA_JSON = """{
  "tables": [
    {
      "name": "students",
      "columns": [
        {"name": "student_id", "data_type": "INTEGER", "nullable": false, "primary_key": true, "foreign_key": null},
        {"name": "student_name", "data_type": "VARCHAR(100)", "nullable": false, "primary_key": false, "foreign_key": null}
      ],
      "primary_key": ["student_id"]
    }
  ],
  "functional_dependencies": [
    {"table": "students", "determinant": ["student_id"], "dependent": ["student_name"]}
  ]
}"""


def test_extract_json_strips_markdown_fences():
    fenced = f"```json\n{SAMPLE_SCHEMA_JSON}\n```"
    assert _extract_json(fenced) == SAMPLE_SCHEMA_JSON.strip()


def test_extract_json_passthrough_when_no_fences():
    assert _extract_json(SAMPLE_SCHEMA_JSON) == SAMPLE_SCHEMA_JSON.strip()


def test_parse_schema_valid_json():
    schema = _parse_schema(SAMPLE_SCHEMA_JSON)
    assert schema.tables[0].name == "students"
    assert schema.functional_dependencies[0].dependent == ["student_name"]


def test_parse_schema_invalid_json_raises():
    with pytest.raises(SchemaGenerationError):
        _parse_schema("not json at all { broken")


def test_parse_schema_wrong_shape_raises():
    with pytest.raises(SchemaGenerationError):
        _parse_schema('{"unexpected_field": true}')
