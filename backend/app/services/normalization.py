"""
Normalization Validation Engine (Module 5) — the project's core technical
contribution.

Given a candidate schema's declared functional dependencies, this module:
  1. Computes attribute closures (fd_closure)
  2. Determines all candidate keys per table (find_candidate_keys) — not
     just the declared primary key, since a table can have multiple valid
     candidate keys (confirmed empirically during generation-module testing,
     e.g. ISBN/email acting as alternate keys)
  3. Checks each table against 1NF, 2NF, 3NF, and BCNF

This is a pure, deterministic algorithm operating only on the declared
FunctionalDependency list — it does not call the LLM and has no dependency
on Module 3. Its correctness guarantee is therefore independent of whether
the LLM "got it right"; it is only as complete as the FDs it is given,
a limitation discussed explicitly in the project's paper.
"""

from __future__ import annotations
from itertools import combinations
from app.models.schema_models import CandidateSchema, TableDef, FunctionalDependency


# ---------------------------------------------------------------------
# Core algorithms
# ---------------------------------------------------------------------

def fd_closure(attrs: set[str], fds: list[FunctionalDependency]) -> set[str]:
    """
    Standard attribute closure algorithm: repeatedly apply any FD whose
    determinant is already covered by the closure, adding its dependents,
    until no further attributes can be added.
    """
    closure = set(attrs)
    changed = True
    while changed:
        changed = False
        for fd in fds:
            det = set(fd.determinant)
            dep = set(fd.dependent)
            if det.issubset(closure) and not dep.issubset(closure):
                closure |= dep
                changed = True
    return closure


def find_candidate_keys(all_attrs: set[str], fds: list[FunctionalDependency]) -> list[set[str]]:
    """
    Finds every minimal candidate key derivable from the declared FDs, by
    searching attribute subsets in increasing size order and skipping any
    subset that is a superset of an already-found (smaller) candidate key.
    This guarantees minimality without needing a separate reduction step.

    Returns an empty list if no subset of all_attrs closes to all_attrs
    (this can happen if the declared FDs don't fully determine the table —
    itself worth surfacing to the caller).
    """
    attrs_list = list(all_attrs)
    candidate_keys: list[set[str]] = []

    for size in range(1, len(attrs_list) + 1):
        for combo in combinations(attrs_list, size):
            combo_set = set(combo)
            if any(ck.issubset(combo_set) for ck in candidate_keys):
                continue  # not minimal — a smaller candidate key already covers this
            if fd_closure(combo_set, fds) == all_attrs:
                candidate_keys.append(combo_set)

    return candidate_keys


def prime_attributes(candidate_keys: list[set[str]]) -> set[str]:
    """Union of all attributes that appear in at least one candidate key."""
    result: set[str] = set()
    for ck in candidate_keys:
        result |= ck
    return result


# ---------------------------------------------------------------------
# Normal form checks
# ---------------------------------------------------------------------

_SUSPICIOUS_1NF_KEYWORDS = ["checklist", "tracker", "tags", "csv", "_list"]


def check_1nf(table: TableDef) -> list[str]:
    """
    Heuristic 1NF check. Atomicity cannot be verified from a schema
    declaration alone (only from actual data), so this flags columns whose
    name and type together *suggest* non-atomic, multi-valued storage
    (e.g. a TEXT column named 'skills_checklist'). This is explicitly a
    heuristic, not a proof, and is documented as such in the project paper.
    """
    issues = []
    for col in table.columns:
        name_lower = col.name.lower()
        dtype_upper = col.data_type.upper()
        looks_textual = "TEXT" in dtype_upper or dtype_upper.startswith("VARCHAR")
        looks_multivalued = any(kw in name_lower for kw in _SUSPICIOUS_1NF_KEYWORDS)
        if looks_textual and looks_multivalued:
            issues.append(
                f"Table '{table.name}': possible 1NF violation — column "
                f"'{col.name}' ({col.data_type}) may store non-atomic, "
                f"multi-valued data based on its name (heuristic check)."
            )
    return issues


def check_2nf(
    table_name: str,
    all_attrs: set[str],
    fds: list[FunctionalDependency],
    candidate_keys: list[set[str]],
) -> list[str]:
    """
    2NF is only relevant when at least one candidate key is composite.
    Violated when a non-prime attribute depends on a proper subset of a
    composite candidate key (a "partial dependency").
    """
    issues = []
    composite_keys = [ck for ck in candidate_keys if len(ck) > 1]
    if not composite_keys:
        return issues

    prime = prime_attributes(candidate_keys)

    for fd in fds:
        det = set(fd.determinant)
        dep = set(fd.dependent)
        for ck in composite_keys:
            if det < ck:  # proper subset
                for attr in dep - prime:
                    issues.append(
                        f"Table '{table_name}': 2NF violation — '{attr}' depends "
                        f"only on {sorted(det)}, a proper subset of candidate "
                        f"key {sorted(ck)}, not the whole key."
                    )
    return issues


def check_3nf(
    table_name: str,
    all_attrs: set[str],
    fds: list[FunctionalDependency],
    candidate_keys: list[set[str]],
) -> list[str]:
    """
    3NF: for every non-trivial FD X -> Y, X must be a superkey, OR every
    attribute in Y must be prime. Violations are transitive dependencies.
    """
    issues = []
    prime = prime_attributes(candidate_keys)

    for fd in fds:
        det = set(fd.determinant)
        dep = set(fd.dependent) - det  # ignore trivial part
        if not dep:
            continue
        is_superkey = fd_closure(det, fds) == all_attrs
        if is_superkey:
            continue
        for attr in dep - prime:
            issues.append(
                f"Table '{table_name}': 3NF violation — transitive dependency "
                f"{sorted(det)} -> '{attr}'; {sorted(det)} is not a superkey and "
                f"'{attr}' is not part of any candidate key."
            )
    return issues


def check_bcnf(
    table_name: str,
    all_attrs: set[str],
    fds: list[FunctionalDependency],
    candidate_keys: list[set[str]],
) -> list[str]:
    """
    BCNF: for every non-trivial FD X -> Y, X must be a superkey — full stop,
    regardless of whether Y is prime. Stricter than 3NF; catches cases like
    overlapping composite candidate keys that 3NF alone permits.
    """
    issues = []
    for fd in fds:
        det = set(fd.determinant)
        dep = set(fd.dependent) - det
        if not dep:
            continue
        is_superkey = fd_closure(det, fds) == all_attrs
        if not is_superkey:
            issues.append(
                f"Table '{table_name}': BCNF violation — {sorted(det)} -> "
                f"{sorted(dep)}, but {sorted(det)} is not a superkey."
            )
    return issues


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------

def validate_normalization(schema: CandidateSchema) -> list[str]:
    """
    Runs all normalization checks across every table in the schema.
    Returns a flat list of human-readable issue descriptions; an empty
    list means no violations were found given the declared dependencies.
    """
    all_issues: list[str] = []

    for table in schema.tables:
        table_fds = [fd for fd in schema.functional_dependencies if fd.table == table.name]
        all_attrs = {c.name for c in table.columns}

        if table_fds:
            candidate_keys = find_candidate_keys(all_attrs, table_fds)
            if not candidate_keys:
                all_issues.append(
                    f"Table '{table.name}': no subset of its declared functional "
                    f"dependencies determines all attributes — unable to confirm "
                    f"any valid candidate key from the declared FDs."
                )
        elif table.primary_key:
            # No FDs declared at all; fall back to the declared PK as the only
            # known key (nothing else can be inferred without any FDs).
            candidate_keys = [set(table.primary_key)]
        else:
            candidate_keys = []

        all_issues.extend(check_1nf(table))
        all_issues.extend(check_2nf(table.name, all_attrs, table_fds, candidate_keys))
        all_issues.extend(check_3nf(table.name, all_attrs, table_fds, candidate_keys))
        all_issues.extend(check_bcnf(table.name, all_attrs, table_fds, candidate_keys))

    return all_issues
