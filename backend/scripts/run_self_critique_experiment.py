"""
Comparison experiment: LLM self-critique (condition C) vs. the
deterministic validator (condition B, measured in run_experiment.py).

This is the missing half of the paper's core evidentiary claim. Having
"our correction loop converges 100% of the time" only proves the
pipeline works — it does not, by itself, show that a deterministic
verifier is BETTER than letting the LLM review its own work, which is
the actual comparison the literature review argues for/against.

Method, per domain:
  1. Generate a fresh baseline schema (same requirements as
     run_experiment.py's DOMAINS, reused directly so both conditions are
     evaluated on the exact same domain set).
  2. Validate it with the real, independent validator (structural +
     normalization) — this is the objective ground truth used to grade
     BOTH conditions B and C. It is never told to the LLM in this script.
  3. If the baseline has zero issues, there's nothing to test self-critique
     against for this domain — recorded as such, not silently skipped.
  4. If the baseline has issues, ask the LLM to review and fix its OWN
     schema (SchemaGenerator.generate_self_critique) with NO hint about
     what is wrong — this is the actual self-critique condition from the
     literature (self-confirmation bias risk).
  5. Re-validate the self-critique's output with the SAME independent
     validator used in step 2. This tells us objectively whether the
     LLM's unaided self-review actually fixed what was wrong, partially
     fixed it, or missed it entirely.

Usage:
    python -m scripts.run_self_critique_experiment

Requires GROQ_API_KEY. Reuses DOMAINS from scripts.run_experiment so the
two conditions are directly comparable on identical requirements text.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from app.services.ai_generation import SchemaGenerator, SchemaGenerationError
from app.services.normalization import validate_normalization
from app.services.structural import validate_structure
from scripts.run_experiment import DOMAINS, _count_violation_types


@dataclass
class SelfCritiqueDomainResult:
    domain: str
    requirements: str
    baseline_issue_count: int
    baseline_issues: list[str]
    violation_types_baseline: dict[str, int]
    self_critique_attempted: bool
    self_critique_final_issue_count: int
    self_critique_final_issues: list[str]
    self_critique_resolved: bool  # True iff final_issue_count == 0
    error: str | None = None


def _validate(schema) -> list[str]:
    return validate_structure(schema) + validate_normalization(schema)


def run_domain_self_critique(name: str, requirements: str) -> SelfCritiqueDomainResult:
    print(f"\n=== [self-critique] Running domain: {name} ===")
    generator = SchemaGenerator()

    try:
        baseline_schema, baseline_raw = generator.generate(requirements)
    except SchemaGenerationError as e:
        print(f"  ERROR: baseline generation failed: {e}")
        return SelfCritiqueDomainResult(
            domain=name, requirements=requirements,
            baseline_issue_count=-1, baseline_issues=[], violation_types_baseline={},
            self_critique_attempted=False, self_critique_final_issue_count=-1,
            self_critique_final_issues=[], self_critique_resolved=False,
            error=str(e),
        )
    except Exception as e:  # noqa: BLE001
        print(f"  ERROR: unexpected failure during baseline generation: {e!r}")
        return SelfCritiqueDomainResult(
            domain=name, requirements=requirements,
            baseline_issue_count=-1, baseline_issues=[], violation_types_baseline={},
            self_critique_attempted=False, self_critique_final_issue_count=-1,
            self_critique_final_issues=[], self_critique_resolved=False,
            error=repr(e),
        )

    baseline_issues = _validate(baseline_schema)
    print(f"  baseline_issues={len(baseline_issues)}")

    if not baseline_issues:
        # Nothing for self-critique to fix — record honestly rather than
        # silently dropping this domain from the comparison.
        return SelfCritiqueDomainResult(
            domain=name, requirements=requirements,
            baseline_issue_count=0, baseline_issues=[], violation_types_baseline={},
            self_critique_attempted=False, self_critique_final_issue_count=0,
            self_critique_final_issues=[], self_critique_resolved=True,
        )

    schema_json = json.dumps(baseline_schema.model_dump(), indent=2)
    try:
        critiqued_schema, _ = generator.generate_self_critique(requirements, schema_json)
    except SchemaGenerationError as e:
        print(f"  ERROR: self-critique call failed to parse: {e}")
        return SelfCritiqueDomainResult(
            domain=name, requirements=requirements,
            baseline_issue_count=len(baseline_issues), baseline_issues=baseline_issues,
            violation_types_baseline=_count_violation_types(baseline_issues),
            self_critique_attempted=True, self_critique_final_issue_count=-1,
            self_critique_final_issues=[], self_critique_resolved=False,
            error=str(e),
        )
    except Exception as e:  # noqa: BLE001
        print(f"  ERROR: unexpected failure during self-critique: {e!r}")
        return SelfCritiqueDomainResult(
            domain=name, requirements=requirements,
            baseline_issue_count=len(baseline_issues), baseline_issues=baseline_issues,
            violation_types_baseline=_count_violation_types(baseline_issues),
            self_critique_attempted=True, self_critique_final_issue_count=-1,
            self_critique_final_issues=[], self_critique_resolved=False,
            error=repr(e),
        )

    final_issues = _validate(critiqued_schema)
    resolved = len(final_issues) == 0
    print(f"  self_critique_final_issues={len(final_issues)} resolved={resolved}")

    return SelfCritiqueDomainResult(
        domain=name, requirements=requirements,
        baseline_issue_count=len(baseline_issues), baseline_issues=baseline_issues,
        violation_types_baseline=_count_violation_types(baseline_issues),
        self_critique_attempted=True,
        self_critique_final_issue_count=len(final_issues),
        self_critique_final_issues=final_issues,
        self_critique_resolved=resolved,
    )


def write_json(results: list[SelfCritiqueDomainResult], path: Path) -> None:
    path.write_text(json.dumps([asdict(r) for r in results], indent=2))


def write_summary_md(results: list[SelfCritiqueDomainResult], path: Path) -> None:
    valid = [r for r in results if r.error is None]
    tested = [r for r in valid if r.self_critique_attempted]  # domains that actually had a baseline violation
    resolved = [r for r in tested if r.self_critique_resolved]

    total_violation_counts: Counter[str] = Counter()
    for r in tested:
        total_violation_counts.update(r.violation_types_baseline)

    lines = []
    lines.append("# Self-Critique Comparison Summary (Condition C)\n")
    lines.append(f"- Domains run: {len(results)} ({len(valid)} valid, {len(results) - len(valid)} errored)")
    lines.append(f"- Domains with a baseline violation (self-critique actually tested): {len(tested)}")
    lines.append(
        f"- Domains where LLM self-critique fully resolved all violations "
        f"(per the independent validator): {len(resolved)}/{len(tested)} "
        f"({(100 * len(resolved) / len(tested)) if tested else 0:.1f}%)"
    )
    lines.append(f"- Baseline violation type breakdown (domains tested): {dict(total_violation_counts)}")
    lines.append("")
    lines.append("## Per-domain results\n")
    lines.append("| Domain | Baseline issues | Self-critique attempted | Final issues | Resolved |")
    lines.append("|---|---|---|---|---|")
    for r in results:
        if r.error:
            lines.append(f"| {r.domain} | ERROR | - | - | - |")
            continue
        lines.append(
            f"| {r.domain} | {r.baseline_issue_count} | "
            f"{'Yes' if r.self_critique_attempted else 'N/A (clean baseline)'} | "
            f"{r.self_critique_final_issue_count} | {'Yes' if r.self_critique_resolved else 'No'} |"
        )
    path.write_text("\n".join(lines) + "\n")


def main():
    import argparse
    import shutil
    from datetime import datetime

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fresh", action="store_true",
        help="Ignore any existing results and re-run all domains from scratch "
             "(the old results file is backed up with a timestamp first, not deleted).",
    )
    args = parser.parse_args()

    results: list[SelfCritiqueDomainResult] = []
    start = time.time()

    out_dir = Path("self_critique_results")
    out_dir.mkdir(exist_ok=True)
    json_path = out_dir / "self_critique_results.json"

    if args.fresh and json_path.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = out_dir / f"self_critique_results.backup_{timestamp}.json"
        shutil.copy(json_path, backup_path)
        print(f"--fresh: backed up existing results to {backup_path}, starting clean.")

    completed: dict[str, SelfCritiqueDomainResult] = {}
    if not args.fresh and json_path.exists():
        try:
            existing = json.loads(json_path.read_text())
            for row in existing:
                if row.get("error") is None:
                    completed[row["domain"]] = SelfCritiqueDomainResult(**row)
            if completed:
                print(f"Resuming: {len(completed)} domain(s) already completed, skipping those.")
        except Exception:
            pass

    for name, requirements in DOMAINS:
        if name in completed:
            results.append(completed[name])
            continue
        result = run_domain_self_critique(name, requirements)
        results.append(result)

        write_json(results, json_path)
        write_summary_md(results, out_dir / "self_critique_summary.md")

        time.sleep(3)

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.1f}s. Wrote results to {out_dir}/")
    print(f"  - {json_path}")
    print(f"  - {out_dir / 'self_critique_summary.md'}")


if __name__ == "__main__":
    main()
