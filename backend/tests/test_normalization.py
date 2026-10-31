"""
Tests for the normalization validation engine (Module 5), built directly
from real outputs observed during AI schema generation testing — both
confirmed-clean schemas and confirmed-violating schemas. Using real traces
(rather than invented examples) means these tests double as evidence that
the engine correctly judges the exact cases the paper's Results section
discusses.
"""

from app.models.schema_models import CandidateSchema, TableDef, ColumnDef, FunctionalDependency
from app.services.normalization import (
    fd_closure,
    find_candidate_keys,
    validate_normalization,
)


def _col(name, dtype="INTEGER", pk=False, fk=None, nullable=True):
    return ColumnDef(name=name, data_type=dtype, nullable=nullable, primary_key=pk, foreign_key=fk)


def _fd(table, det, dep):
    return FunctionalDependency(table=table, determinant=det, dependent=dep)


# ---------------------------------------------------------------------
# Core algorithm sanity checks
# ---------------------------------------------------------------------

def test_closure_basic():
    fds = [_fd("t", ["a"], ["b"]), _fd("t", ["b"], ["c"])]
    assert fd_closure({"a"}, fds) == {"a", "b", "c"}


def test_candidate_keys_multiple_alternate_keys():
    # Mirrors the library 'books' table: id is PK, isbn is an alternate key
    all_attrs = {"id", "isbn", "title"}
    fds = [
        _fd("books", ["id"], ["isbn", "title"]),
        _fd("books", ["isbn"], ["title"]),  # NOTE: isbn doesn't determine id here on its own
    ]
    # isbn alone can't reach 'id', so only {id} is a valid candidate key in this
    # reduced example — full multi-key case tested separately below.
    keys = find_candidate_keys(all_attrs, fds)
    assert {"id"} in keys


def test_candidate_keys_true_alternate_key():
    # If isbn -> id is ALSO declared (as the real generation output did),
    # isbn becomes a second valid candidate key.
    all_attrs = {"id", "isbn", "title"}
    fds = [
        _fd("books", ["id"], ["isbn", "title"]),
        _fd("books", ["isbn"], ["id", "title"]),
    ]
    keys = find_candidate_keys(all_attrs, fds)
    assert {"id"} in keys
    assert {"isbn"} in keys


# ---------------------------------------------------------------------
# CLEAN schemas (confirmed no violation during generation testing)
# ---------------------------------------------------------------------

def test_clean_student_course_enrollment():
    schema = CandidateSchema(
        tables=[
            TableDef(name="students", columns=[_col("student_id", pk=True), _col("name", "VARCHAR(100)")],
                     primary_key=["student_id"]),
            TableDef(name="courses", columns=[_col("course_id", pk=True), _col("title", "VARCHAR(200)")],
                     primary_key=["course_id"]),
            TableDef(name="enrollments",
                     columns=[_col("student_id", fk="students.student_id"),
                              _col("course_id", fk="courses.course_id"),
                              _col("grade", "DECIMAL(5,2)")],
                     primary_key=["student_id", "course_id"]),
        ],
        functional_dependencies=[
            _fd("students", ["student_id"], ["name"]),
            _fd("courses", ["course_id"], ["title"]),
            _fd("enrollments", ["student_id", "course_id"], ["grade"]),
        ],
    )
    assert validate_normalization(schema) == []


def test_clean_hospital_appointments():
    # From real generation output: doctor's department stored only on
    # doctors, never duplicated onto appointments.
    schema = CandidateSchema(
        tables=[
            TableDef(name="departments", columns=[_col("department_id", pk=True), _col("name", "VARCHAR(100)")],
                     primary_key=["department_id"]),
            TableDef(name="doctors",
                     columns=[_col("doctor_id", pk=True), _col("name", "VARCHAR(100)"),
                              _col("department_id", fk="departments.department_id")],
                     primary_key=["doctor_id"]),
            TableDef(name="appointments",
                     columns=[_col("appointment_id", pk=True), _col("doctor_id", fk="doctors.doctor_id"),
                              _col("appointment_datetime", "DATETIME")],
                     primary_key=["appointment_id"]),
        ],
        functional_dependencies=[
            _fd("departments", ["department_id"], ["name"]),
            _fd("doctors", ["doctor_id"], ["name", "department_id"]),
            _fd("appointments", ["appointment_id"], ["doctor_id", "appointment_datetime"]),
        ],
    )
    assert validate_normalization(schema) == []


# ---------------------------------------------------------------------
# CONFIRMED VIOLATIONS from real generation testing
# ---------------------------------------------------------------------

def test_real_estate_listings_agent_branch_violation():
    """
    Real confirmed case: agent_name -> branch_office, PK = property_id.
    agent_name is a single non-composite non-superkey determinant —
    the simplest possible 3NF/BCNF violation shape.
    """
    schema = CandidateSchema(
        tables=[
            TableDef(name="listings",
                     columns=[_col("property_id", pk=True), _col("agent_name", "VARCHAR(100)"),
                              _col("branch_office", "VARCHAR(100)")],
                     primary_key=["property_id"]),
        ],
        functional_dependencies=[
            _fd("listings", ["property_id"], ["agent_name", "branch_office"]),
            _fd("listings", ["agent_name"], ["branch_office"]),
        ],
    )
    issues = validate_normalization(schema)
    assert any("3NF violation" in i for i in issues)
    assert any("BCNF violation" in i for i in issues)


def test_therapist_branch_violation():
    """Real confirmed case: therapist_id -> clinic_branch_id, PK = appointment_id."""
    schema = CandidateSchema(
        tables=[
            TableDef(name="appointments",
                     columns=[_col("appointment_id", pk=True), _col("patient_id"), _col("therapist_id"),
                              _col("clinic_branch_id"), _col("appointment_time", "DATETIME")],
                     primary_key=["appointment_id"]),
        ],
        functional_dependencies=[
            _fd("appointments", ["appointment_id"], ["patient_id", "therapist_id", "clinic_branch_id", "appointment_time"]),
            _fd("appointments", ["therapist_id"], ["clinic_branch_id"]),
        ],
    )
    issues = validate_normalization(schema)
    assert any("3NF violation" in i and "clinic_branch_id" in i for i in issues)


def test_shipping_zone_redundant_city_violation():
    """Real confirmed case: shipping_zone_code -> destination_city redundantly
    kept on shipments, even though shipping_zones already normalizes it."""
    schema = CandidateSchema(
        tables=[
            TableDef(name="shipping_zones",
                     columns=[_col("zone_code", "VARCHAR(20)", pk=True), _col("destination_city", "VARCHAR(100)")],
                     primary_key=["zone_code"]),
            TableDef(name="shipments",
                     columns=[_col("shipment_id", "VARCHAR(50)", pk=True), _col("origin_zip_code", "VARCHAR(20)"),
                              _col("destination_city", "VARCHAR(100)"),
                              _col("shipping_zone_code", "VARCHAR(20)", fk="shipping_zones.zone_code")],
                     primary_key=["shipment_id"]),
        ],
        functional_dependencies=[
            _fd("shipping_zones", ["zone_code"], ["destination_city"]),
            _fd("shipments", ["shipment_id"], ["origin_zip_code", "destination_city", "shipping_zone_code"]),
            _fd("shipments", ["shipping_zone_code"], ["destination_city"]),
        ],
    )
    issues = validate_normalization(schema)
    shipments_issues = [i for i in issues if "'shipments'" in i]
    zones_issues = [i for i in issues if "'shipping_zones'" in i]
    assert any("3NF violation" in i for i in shipments_issues)
    assert zones_issues == []  # shipping_zones itself is correctly normalized


def test_patient_conditions_tracker_1nf_heuristic():
    """Real confirmed case: 'conditions_tracker' TEXT column storing a
    comma-separated list of chronic conditions — a 1NF violation."""
    schema = CandidateSchema(
        tables=[
            TableDef(name="patients",
                     columns=[_col("patient_id", pk=True), _col("full_name", "VARCHAR(200)"),
                              _col("date_of_birth", "DATE"), _col("conditions_tracker", "TEXT")],
                     primary_key=["patient_id"]),
        ],
        functional_dependencies=[
            _fd("patients", ["patient_id"], ["full_name", "date_of_birth", "conditions_tracker"]),
        ],
    )
    issues = validate_normalization(schema)
    assert any("1NF violation" in i and "conditions_tracker" in i for i in issues)


def test_training_records_instructor_room_violation():
    """Real confirmed case: composite key (employee_id, course_id), with
    instructor_name -> instructor_room_number smuggled in as a single-
    attribute, non-superkey determinant."""
    schema = CandidateSchema(
        tables=[
            TableDef(name="training_records",
                     columns=[_col("employee_id", pk=True), _col("course_id", pk=True),
                              _col("completion_status", "VARCHAR(20)"), _col("instructor_name", "VARCHAR(100)"),
                              _col("instructor_room_number", "VARCHAR(20)")],
                     primary_key=["employee_id", "course_id"]),
        ],
        functional_dependencies=[
            _fd("training_records", ["employee_id", "course_id"],
                ["completion_status", "instructor_name", "instructor_room_number"]),
            _fd("training_records", ["instructor_name"], ["instructor_room_number"]),
        ],
    )
    issues = validate_normalization(schema)
    assert any("3NF violation" in i and "instructor_room_number" in i for i in issues)


def test_flat_student_records_multiple_violations():
    """Real confirmed case: student_records flattened to one table with
    three independent transitive dependencies (major, department, advisor)."""
    schema = CandidateSchema(
        tables=[
            TableDef(name="student_records",
                     columns=[_col("student_id", pk=True), _col("student_name", "VARCHAR(100)"),
                              _col("major_id"), _col("major_name", "VARCHAR(100)"),
                              _col("department_id"), _col("department_name", "VARCHAR(100)"),
                              _col("office_location", "VARCHAR(100)"), _col("advisor_id"),
                              _col("advisor_name", "VARCHAR(100)")],
                     primary_key=["student_id"]),
        ],
        functional_dependencies=[
            _fd("student_records", ["student_id"],
                ["student_name", "major_id", "major_name", "department_id", "department_name",
                 "office_location", "advisor_id", "advisor_name"]),
            _fd("student_records", ["major_id"], ["major_name"]),
            _fd("student_records", ["department_id"], ["department_name", "office_location"]),
            _fd("student_records", ["advisor_id"], ["advisor_name"]),
        ],
    )
    issues = validate_normalization(schema)
    violating_attrs = {"major_name", "department_name", "office_location", "advisor_name"}
    flagged = {attr for attr in violating_attrs if any(attr in i for i in issues)}
    assert flagged == violating_attrs, f"Expected all of {violating_attrs} to be flagged, got {flagged}"


def test_consultant_role_bcnf_violation():
    """
    Real confirmed case: assignments(project_id, consultant_id) composite PK,
    with consultant_id -> role_id declared. Only {project_id, consultant_id}
    closes to all attributes here (role_id alone doesn't determine project_id,
    and neither does consultant_id), so this is both a 3NF and BCNF violation
    since role_id is not part of the single candidate key.
    """
    schema = CandidateSchema(
        tables=[
            TableDef(name="assignments",
                     columns=[_col("project_id", pk=True), _col("consultant_id", pk=True), _col("role_id")],
                     primary_key=["project_id", "consultant_id"]),
        ],
        functional_dependencies=[
            _fd("assignments", ["project_id", "consultant_id"], ["role_id"]),
            _fd("assignments", ["consultant_id"], ["role_id"]),
        ],
    )
    issues = validate_normalization(schema)
    assert any("BCNF violation" in i and "role_id" in i for i in issues)
    assert any("3NF violation" in i and "role_id" in i for i in issues)


def test_non_deterministic_case_with_dependency_declared():
    """
    Real confirmed case: the employee_certifications prompt sometimes
    produced authority_name -> authority_city (a violation) and sometimes
    omitted it (no violation, per declared FDs). This test locks in the
    'violation present' branch of that observed non-determinism.
    """
    schema = CandidateSchema(
        tables=[
            TableDef(name="employee_certifications",
                     columns=[_col("employee_id", pk=True), _col("certificate_code", "VARCHAR(20)", pk=True),
                              _col("completion_date", "DATE"), _col("authority_name", "VARCHAR(100)"),
                              _col("authority_city", "VARCHAR(100)")],
                     primary_key=["employee_id", "certificate_code"]),
        ],
        functional_dependencies=[
            _fd("employee_certifications", ["employee_id", "certificate_code"],
                ["completion_date", "authority_name", "authority_city"]),
            _fd("employee_certifications", ["authority_name"], ["authority_city"]),
        ],
    )
    issues = validate_normalization(schema)
    assert any("authority_city" in i for i in issues)


def test_non_deterministic_case_without_dependency_declared():
    """Same prompt, other observed run: no secondary FD declared, so the
    engine correctly reports zero violations given what was declared —
    this is the documented declared-dependency-completeness limitation,
    not an engine bug."""
    schema = CandidateSchema(
        tables=[
            TableDef(name="employee_certifications",
                     columns=[_col("employee_id", pk=True), _col("certificate_code", "VARCHAR(20)", pk=True),
                              _col("completion_date", "DATE"), _col("authority_name", "VARCHAR(100)"),
                              _col("authority_city", "VARCHAR(100)")],
                     primary_key=["employee_id", "certificate_code"]),
        ],
        functional_dependencies=[
            _fd("employee_certifications", ["employee_id", "certificate_code"],
                ["completion_date", "authority_name", "authority_city"]),
        ],
    )
    issues = validate_normalization(schema)
    assert issues == []


# ---------------------------------------------------------------------
# CONFIRMED VIOLATIONS from try_generation prompt set
# (each mirrors an exact `python -m scripts.try_generation "..."` case)
# ---------------------------------------------------------------------

def test_sports_clinic_therapist_branch_violation():
    """
    Prompt: single table for a sports clinic's appointment sessions, where
    a therapist works at exactly one clinic branch but each branch has many
    therapists. therapist_id -> clinic_branch_id is a non-superkey
    determinant on a single-column PK table — 3NF/BCNF violation.
    """
    schema = CandidateSchema(
        tables=[
            TableDef(name="appointment_sessions",
                     columns=[_col("session_id", pk=True), _col("patient_id"),
                              _col("time_slot", "DATETIME"), _col("therapist_id"),
                              _col("clinic_branch_id")],
                     primary_key=["session_id"]),
        ],
        functional_dependencies=[
            _fd("appointment_sessions", ["session_id"],
                ["patient_id", "time_slot", "therapist_id", "clinic_branch_id"]),
            _fd("appointment_sessions", ["therapist_id"], ["clinic_branch_id"]),
        ],
    )
    issues = validate_normalization(schema)
    assert any("3NF violation" in i and "clinic_branch_id" in i for i in issues)
    assert any("BCNF violation" in i and "clinic_branch_id" in i for i in issues)


def test_car_rental_insurance_premium_violation():
    """
    Prompt: car rental table storing Rental ID, Car Model, Customer Name,
    Insurance Package Type, and that package's exact Premium Cost.
    insurance_package_type -> insurance_premium_cost — the premium is a
    property of the *package type*, not of the rental itself, so it's
    transitively dependent on a non-superkey attribute.
    """
    schema = CandidateSchema(
        tables=[
            TableDef(name="rentals",
                     columns=[_col("rental_id", pk=True), _col("car_model", "VARCHAR(100)"),
                              _col("customer_name", "VARCHAR(100)"),
                              _col("insurance_package_type", "VARCHAR(50)"),
                              _col("insurance_premium_cost", "DECIMAL(10,2)")],
                     primary_key=["rental_id"]),
        ],
        functional_dependencies=[
            _fd("rentals", ["rental_id"],
                ["car_model", "customer_name", "insurance_package_type", "insurance_premium_cost"]),
            _fd("rentals", ["insurance_package_type"], ["insurance_premium_cost"]),
        ],
    )
    issues = validate_normalization(schema)
    assert any("3NF violation" in i and "insurance_premium_cost" in i for i in issues)
    assert any("BCNF violation" in i and "insurance_premium_cost" in i for i in issues)


def test_real_estate_county_tax_rate_violation():
    """
    Prompt: property listing table storing Property ID, Address, Square
    Footage, County Name, and County Property Tax Rate. The tax rate is a
    property of the *county*, not of the individual listing —
    county_name -> county_property_tax_rate is a transitive dependency.
    """
    schema = CandidateSchema(
        tables=[
            TableDef(name="property_listings",
                     columns=[_col("property_id", pk=True), _col("address", "VARCHAR(200)"),
                              _col("square_footage", "INTEGER"), _col("county_name", "VARCHAR(100)"),
                              _col("county_property_tax_rate", "DECIMAL(5,4)")],
                     primary_key=["property_id"]),
        ],
        functional_dependencies=[
            _fd("property_listings", ["property_id"],
                ["address", "square_footage", "county_name", "county_property_tax_rate"]),
            _fd("property_listings", ["county_name"], ["county_property_tax_rate"]),
        ],
    )
    issues = validate_normalization(schema)
    assert any("3NF violation" in i and "county_property_tax_rate" in i for i in issues)
    assert any("BCNF violation" in i and "county_property_tax_rate" in i for i in issues)


def test_inventory_matrix_composite_key_2nf_violation():
    """
    Prompt: inventory_matrix table with a composite PK (Product ID,
    Supplier ID), storing Supplier Phone Number and Stock Quantity.
    supplier_phone_number depends only on supplier_id — a proper subset of
    the composite key — which is a textbook partial dependency (2NF
    violation), not just a 3NF/BCNF one. This is the first planted-violation
    test that specifically exercises check_2nf.
    """
    schema = CandidateSchema(
        tables=[
            TableDef(name="inventory_matrix",
                     columns=[_col("product_id", pk=True), _col("supplier_id", pk=True),
                              _col("supplier_phone_number", "VARCHAR(20)"),
                              _col("stock_quantity", "INTEGER")],
                     primary_key=["product_id", "supplier_id"]),
        ],
        functional_dependencies=[
            _fd("inventory_matrix", ["product_id", "supplier_id"],
                ["supplier_phone_number", "stock_quantity"]),
            _fd("inventory_matrix", ["supplier_id"], ["supplier_phone_number"]),
        ],
    )
    issues = validate_normalization(schema)
    assert any("2NF violation" in i and "supplier_phone_number" in i for i in issues)


def test_gymnastics_skills_checklist_1nf_violation():
    """
    Prompt: gymnastics member table with a checklist string column
    listing all mastered skills (e.g. "Handstand, Backflip, Cartwheel")
    crammed into one TEXT field — a classic 1NF atomicity violation,
    caught by the naming heuristic ('checklist' + TEXT).
    """
    schema = CandidateSchema(
        tables=[
            TableDef(name="members",
                     columns=[_col("member_id", pk=True), _col("full_name", "VARCHAR(100)"),
                              _col("class_level", "VARCHAR(50)"), _col("skills_checklist", "TEXT")],
                     primary_key=["member_id"]),
        ],
        functional_dependencies=[
            _fd("members", ["member_id"], ["full_name", "class_level", "skills_checklist"]),
        ],
    )
    issues = validate_normalization(schema)
    assert any("1NF violation" in i and "skills_checklist" in i for i in issues)
