"""
Experiment runner for the paper's core empirical claim:

  "Decoupling schema generation from schema verification, using a
  deterministic algorithmic verifier rather than an LLM-based
  self-critique, produces measurably more correct database schemas than
  unverified AI-based generation alone."

This script is the actual experimental method, not a demo. It runs every
domain prompt in DOMAINS through the real generate -> validate -> correct
pipeline (Module 6), in-process (no manual copy/pasting of terminal
output), and writes structured results to disk:

  - experiment_results.json  — full per-domain record (every attempt,
    every issue string, for traceability/appendix material)
  - experiment_results.csv   — one row per domain, the numbers that go
    directly into a Results table
  - experiment_summary.md    — aggregate statistics + a ready-to-paste
    markdown table

Usage:
    python -m scripts.run_experiment

Requires GROQ_API_KEY to be set (via .env or environment variable). Each
domain makes 1-4 real LLM calls (1 generation + up to max_correction_attempts
corrections), so expect ~15-70 API calls for the full DOMAINS list.
"""

from __future__ import annotations

import csv
import json
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from app.services.ai_generation import SchemaGenerator, SchemaGenerationError
from app.services.correction_loop import run_correction_loop

# ---------------------------------------------------------------------
# Domain set. Each entry is (short_name, requirements_prompt).
#
# The first 8 are the domains already manually validated during
# development (see chat history / thesis notes) — kept here verbatim so
# re-running this script reproduces the exact same experiment, not a
# re-sampled one. Add more entries to reach the 15-20 domains the
# methodology calls for; keep short_name filesystem/CSV-safe (no commas).
# ---------------------------------------------------------------------

DOMAINS: list[tuple[str, str]] = [
    (
        "sports_clinic_therapist_branch",
        "I need a database for a sports clinic to manage therapist appointments. "
        "Each appointment session has a specific Patient, a Time slot, and an "
        "assigned Therapist. A therapist only works at one specific Clinic Branch, "
        "but our clinic branches have multiple therapists working in them. Please "
        "design a single table to store these sessions.",
    ),
    (
        "project_assignment_costs",
        "We need a table named project_assignment_costs to track project expenses. "
        "Store worker_id, project_id, location_id, hours_worked, hourly_rate, and "
        "location_tax_rate. The primary key is a composite of worker_id, project_id, "
        "and location_id. Note that location_tax_rate depends only on location_id, "
        "while hourly_rate depends on the worker_id and location_id combination.",
    ),
    (
        "consulting_assignments",
        "I need a database layout for our corporate consulting assignments. Each "
        "assignment tracks a unique Project, the Consultant assigned, and the "
        "specific Role they perform. Note that a Consultant can only hold a single "
        "specific Role across our entire firm, but multiple consultants can share "
        "the same Role on the same Project.",
    ),
    (
        "hospital_shifts",
        "Design a scheduling schema for a medical hospital network. Each row tracks "
        "a unique Shift ID, an Assigned Doctor, and the specific Medical Ward they "
        "are operating in. Note that a Doctor is permanently bound to a single "
        "Medical Ward across our network, but multiple doctors work shifts in the "
        "same ward.",
    ),
    (
        "real_estate_listings",
        "I need a database layout for our real estate firm to track listings. Each "
        "listing tracks a Property ID, the Assigned Agent Name, and the Agent's "
        "Local Branch Office. Note that an Agent Name uniquely determines which "
        "single Local Branch Office they operate out of firm-wide, but a Local "
        "Branch Office can host multiple different agents. Set Property ID as the "
        "primary key.",
    ),
    (
        "university_classes",
        "Design a database layout to track university classes. Each record maps a "
        "unique Class ID, an Assigned Professor, and the Classroom Code they use. "
        "Note that a Professor is permanently assigned to exactly one specific "
        "Classroom Code across the campus, but a Classroom Code can host different "
        "professors for different classes.",
    ),
    (
        "inventory_matrix",
        "I need a single table called inventory_matrix to manage our online store "
        "inventory. Each row needs a composite primary key made of Product ID and "
        "Supplier ID. Inside the row, save the Supplier Phone Number and the Stock "
        "Quantity of that specific product from that supplier.",
    ),
    (
        "gymnastics_skills_checklist",
        "Design a database for a gymnastics school. For every student member, I "
        "want to record their Member ID, Full Name, Class Level, and a checklist "
        "string containing all the skills they have successfully mastered so far "
        "(e.g., Handstand, Backflip, Cartwheel).",
    ),
    (
        "car_rental_insurance",
        "Create a database for car rentals. For each rental transaction, I want "
        "to store the Rental ID, the Car Model, the Customer Name, the Selected "
        "Insurance Package Type, and the exact Insurance Premium Cost of that "
        "specific package.",
    ),
    (
        "real_estate_county_tax_rate",
        "Design a real estate database. For each property listing, I want to log "
        "the Property ID, the Address, the Square Footage, the County Name, and "
        "the County Property Tax Rate.",
    ),
    (
        "cargo_routing_shipping_zone",
        "Build a table for an international logistics firm to track cargo "
        "routing. Each tracking row stores the Shipment ID, the Origin Zip Code, "
        "the Destination City, and the assigned Shipping Zone Code. Note that a "
        "Shipping Zone Code maps to exactly one specific Destination City.",
    ),
    (
        "checkout_coupon_2nf",
        "Design a checkout table mapping Order ID and Coupon Code as a combined "
        "primary key. For each record, save the Order Total Price, the Coupon "
        "Discount Percentage, and the Coupon Expiration Date.",
    ),
    (
        "distribution_center_bin_placement",
        "I need a database to map inventory item placement inside our "
        "distribution center. Each entry tracks the Placement ID, the Item Name, "
        "the Row Number, the Bin Number, the Bin Width, and the Bin Depth.",
    ),
    (
        "project_roster_department",
        "I need a single table called project_roster to manage employee "
        "allocations. Each row should use a composite key made up of Employee ID "
        "and Project ID. For each row, record the Hours Worked on that specific "
        "project, the Employee's Department ID, and the Department Name.",
    ),
    (
        "inventory_batches_storage_zone_temp",
        "I need a single tracking called inventory_batches. For each product "
        "batch, record the Batch ID, Product Name, Expiration Date, Assigned "
        "Storage Zone Code, Warehouse Room Temperature requirement, and the "
        "Quantity in stock. Note that a Storage Zone Code explicitly determines "
        "its own Warehouse Room Temperature requirement.",
    ),
    (
        "library_books_publisher_hq",
        "Design a flat catalog table named library_books. It must log the Book "
        "ISBN, Title, Author Name, Publisher Name, and the Publisher's Corporate "
        "Headquarter City.",
    ),
    (
        "consultancy_skills_projects_mvd",
        # NOTE: this is a 4NF / multi-valued dependency case, not 1NF-BCNF.
        # validate_normalization() only checks 1NF/2NF/3NF/BCNF, so this domain
        # is EXPECTED to come back with 0 issues despite a real design flaw
        # (two independent multi-valued facts crammed into one table). Keep
        # this domain in the set specifically to document that scope
        # limitation with a real trace, not to test convergence.
        "I need a tracking database for a consultancy. For each Employee ID, I "
        "want to store the multiple independent software technologies they are "
        "skilled in, and independently, a list of the various client project "
        "codes they are currently assigned to.",
    ),
    (
        "manufacturing_log_component_manufacturer",
        "Create a table called manufacturing_log to monitor our assembly line. "
        "Each record must use a composite key of Component Serial Number and "
        "Factory Batch ID. For each entry, store the assembly duration, the "
        "Component Manufacturer Name, and the Manufacturer Headquarter City.",
    ),
    (
        "patient_visits_doctor_specialty",
        "Design a clinic database table called patient_visits. For each visit "
        "row, I want to store the Visit ID, the Patient Name, the Doctor Name, "
        "the Doctor's Department Specialty (like Pediatrics or Cardiology), and "
        "the Department Floor Location.",
    ),
    (
        "movie_catalog_studio_hq",
        "Create a single flat table called movie_catalog. It must store the "
        "Movie Title, Release Year, Production Studio Name, and the Studio's "
        "Corporate Headquarters Country.",
    ),
    (
        "course_enrollments_2nf",
        # Prompts #16 and #17 in the audit were identical; included once here.
        "Design a flat table named course_enrollments to track student "
        "registrations. Identify rows using a composite key of student_id and "
        "course_id. For each row, include student_name, course_title, and "
        "final_grade.",
    ),
    (
        "rental_units_zip_city_state",
        "We are designing a property management database to track rental units. "
        "We need to store unit_id, property_address, zip_code, city, state, and "
        "monthly_rent. Set unit_id as the primary key.",
    ),
    (
        "ecommerce_sales_region_rep",
        "Create a database layout for our e-commerce site sales. For each "
        "purchase, store the Order ID, Product SKU, Quantity, Customer Email, "
        "Customer Region Group, and Region Sales Representative Name. Set Order "
        "ID as the primary key.",
    ),
    (
        "maintenance_log_station_manager",
        "Design a table named maintenance_log. For each repair service, record "
        "the Log ID, Vehicle VIN, Mechanic Name, Service Station City, and the "
        "Regional Manager Name who oversees that Service Station City. Set Log "
        "ID as the primary key.",
    ),
    (
        "employee_shifts_branch_tax_id",
        "Create a flat table called employee_shifts. For each shift tracked, "
        "record the Shift ID, Employee Name, Assigned Office Branch Code, and "
        "the specific Branch Regional Tax Identification Number. Set Shift ID as "
        "the primary key.",
    ),

    # --- Add more domains below to reach 15-20 total ---
    # ("short_name", "requirements text..."),
]


# ---------------------------------------------------------------------
# Per-domain record
# ---------------------------------------------------------------------

@dataclass
class DomainResult:
    domain: str
    requirements: str
    baseline_issue_count: int
    final_issue_count: int
    converged: bool
    correction_rounds_used: int
    generation_failed_partway: bool
    baseline_issues: list[str]
    final_issues: list[str]
    violation_types_baseline: dict[str, int]  # e.g. {"1NF": 1, "3NF": 2, "BCNF": 2}
    error: str | None = None  # set if the domain crashed entirely (not just non-converged)


_VIOLATION_TYPE_PATTERN = re.compile(r"\b(1NF|2NF|3NF|BCNF)\b")


def _count_violation_types(issues: list[str]) -> dict[str, int]:
    """Tally how many issue strings mention each normal form, so the paper
    can report a breakdown like '1NF: 1, 2NF: 2, 3NF: 5, BCNF: 5' rather
    than just a flat issue count."""
    counts: Counter[str] = Counter()
    for issue in issues:
        match = _VIOLATION_TYPE_PATTERN.search(issue)
        if match:
            counts[match.group(1)] += 1
    return dict(counts)


def run_domain(name: str, requirements: str) -> DomainResult:
    print(f"\n=== Running domain: {name} ===")
    generator = SchemaGenerator()
    try:
        result = run_correction_loop(requirements, generator=generator)
    except SchemaGenerationError as e:
        print(f"  ERROR: generation failed (even after retries): {e}")
        return DomainResult(
            domain=name,
            requirements=requirements,
            baseline_issue_count=-1,
            final_issue_count=-1,
            converged=False,
            correction_rounds_used=0,
            generation_failed_partway=True,
            baseline_issues=[],
            final_issues=[],
            violation_types_baseline={},
            error=str(e),
        )
    except Exception as e:  # noqa: BLE001 - deliberately broad: one bad
        # domain (network blip, unexpected API response shape, etc.) must
        # never take down the whole batch run and lose every other
        # domain's already-collected data.
        print(f"  ERROR: unexpected failure on this domain: {e!r}")
        return DomainResult(
            domain=name,
            requirements=requirements,
            baseline_issue_count=-1,
            final_issue_count=-1,
            converged=False,
            correction_rounds_used=0,
            generation_failed_partway=True,
            baseline_issues=[],
            final_issues=[],
            violation_types_baseline={},
            error=repr(e),
        )

    generation_failed_partway = any(a.generation_failed for a in result.attempts)
    baseline_issues = result.attempts[0].all_issues
    final_issues = result.final_attempt.all_issues

    print(
        f"  baseline_issues={result.baseline_issue_count} "
        f"converged={result.converged} rounds_used={result.correction_rounds_used}"
    )

    return DomainResult(
        domain=name,
        requirements=requirements,
        baseline_issue_count=result.baseline_issue_count,
        final_issue_count=result.final_issue_count,
        converged=result.converged,
        correction_rounds_used=result.correction_rounds_used,
        generation_failed_partway=generation_failed_partway,
        baseline_issues=baseline_issues,
        final_issues=final_issues,
        violation_types_baseline=_count_violation_types(baseline_issues),
    )


# ---------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------

def write_json(results: list[DomainResult], path: Path) -> None:
    path.write_text(json.dumps([asdict(r) for r in results], indent=2))


def write_csv(results: list[DomainResult], path: Path) -> None:
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "domain", "baseline_issue_count", "final_issue_count", "converged",
            "correction_rounds_used", "generation_failed_partway",
            "1NF", "2NF", "3NF", "BCNF", "error",
        ])
        for r in results:
            vt = r.violation_types_baseline
            writer.writerow([
                r.domain, r.baseline_issue_count, r.final_issue_count, r.converged,
                r.correction_rounds_used, r.generation_failed_partway,
                vt.get("1NF", 0), vt.get("2NF", 0), vt.get("3NF", 0), vt.get("BCNF", 0),
                r.error or "",
            ])


def write_summary_md(results: list[DomainResult], path: Path) -> None:
    valid = [r for r in results if r.error is None]
    n = len(results)
    n_valid = len(valid)
    n_converged = sum(1 for r in valid if r.converged)
    n_needed_correction = sum(1 for r in valid if r.baseline_issue_count > 0)
    n_clean_baseline = sum(1 for r in valid if r.baseline_issue_count == 0)
    rounds_when_needed = [r.correction_rounds_used for r in valid if r.baseline_issue_count > 0]
    avg_rounds = (sum(rounds_when_needed) / len(rounds_when_needed)) if rounds_when_needed else 0.0

    total_violation_counts: Counter[str] = Counter()
    for r in valid:
        total_violation_counts.update(r.violation_types_baseline)

    lines = []
    lines.append("# Experiment Summary\n")
    lines.append(f"- Domains run: {n} ({n_valid} valid, {n - n_valid} failed to generate at all)")
    lines.append(f"- Domains with a clean baseline (0 issues, no correction needed): {n_clean_baseline}")
    lines.append(f"- Domains requiring correction: {n_needed_correction}")
    lines.append(f"- Domains that converged (0 issues after correction, bounded by max_correction_attempts): {n_converged}/{n_valid}")
    lines.append(f"- Average correction rounds used (among domains that needed correction): {avg_rounds:.2f}")
    lines.append(f"- Baseline violation type breakdown (summed across all domains): {dict(total_violation_counts)}")
    lines.append("")
    lines.append("## Per-domain results\n")
    lines.append("| Domain | Baseline issues | Converged | Rounds used | 1NF | 2NF | 3NF | BCNF |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in results:
        if r.error:
            lines.append(f"| {r.domain} | ERROR | - | - | - | - | - | - |")
            continue
        vt = r.violation_types_baseline
        lines.append(
            f"| {r.domain} | {r.baseline_issue_count} | {'Yes' if r.converged else 'No'} | "
            f"{r.correction_rounds_used} | {vt.get('1NF', 0)} | {vt.get('2NF', 0)} | "
            f"{vt.get('3NF', 0)} | {vt.get('BCNF', 0)} |"
        )
    path.write_text("\n".join(lines) + "\n")


def main():
    results: list[DomainResult] = []
    start = time.time()

    out_dir = Path("experiment_results")
    out_dir.mkdir(exist_ok=True)

    # Resume support: if a previous run already produced results for some
    # domains (e.g. the run crashed or was rate-limited partway through),
    # skip re-running those and start from where it left off. This is what
    # actually matters for a batch of 25 real API-backed domains — losing
    # 6 domains' worth of completed work to one later failure is expensive
    # in both time and API quota.
    json_path = out_dir / "experiment_results.json"
    completed: dict[str, DomainResult] = {}
    if json_path.exists():
        try:
            existing = json.loads(json_path.read_text())
            for row in existing:
                if row.get("error") is None:  # only trust successful runs
                    completed[row["domain"]] = DomainResult(**row)
            if completed:
                print(f"Resuming: {len(completed)} domain(s) already completed, skipping those.")
        except Exception:
            pass  # if the file is corrupt/old-format, just start fresh

    for name, requirements in DOMAINS:
        if name in completed:
            results.append(completed[name])
            continue
        domain_result = run_domain(name, requirements)
        results.append(domain_result)

        # Write after every domain, not just at the end, so a crash or
        # rate-limit failure on domain N never costs you domains 1..N-1.
        write_json(results, json_path)
        write_csv(results, out_dir / "experiment_results.csv")
        write_summary_md(results, out_dir / "experiment_summary.md")

        # Small pause between domains to stay under low free-tier
        # tokens-per-minute limits, on top of the per-call retry/backoff
        # already inside _call_llm.
        time.sleep(3)

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.1f}s. Wrote results to {out_dir}/")
    print(f"  - {out_dir / 'experiment_results.json'} (full detail)")
    print(f"  - {out_dir / 'experiment_results.csv'} (spreadsheet-ready)")
    print(f"  - {out_dir / 'experiment_summary.md'} (paste-ready summary + table)")


if __name__ == "__main__":
    main()
