"""
Manual test for the full generate -> validate -> correct pipeline
(Module 6), without needing the FastAPI server. Usage:

    python -m scripts.try_correction_loop "I need a system to track \
students, the courses they enroll in, and their grades"

Prints each attempt's issue count so you can see the correction loop
converge (or fail to) in real time — this is also the shape of data your
paper's Results section needs (baseline vs. corrected violation counts).

Requires GROQ_API_KEY to be set (via .env or environment variable).
"""

import sys
import json

from app.services.correction_loop import run_correction_loop


def main():
    if len(sys.argv) < 2:
        print('Usage: python -m scripts.try_correction_loop "your requirements here"')
        sys.exit(1)

    requirements = sys.argv[1]

    print(f"Requirements: {requirements}\n")
    print("Running generate -> validate -> correct pipeline...\n")

    result = run_correction_loop(requirements)

    for attempt in result.attempts:
        label = "Initial generation" if attempt.attempt_number == 0 else f"Correction round {attempt.attempt_number}"
        status = "FAILED TO PARSE" if attempt.generation_failed else (
            "CLEAN" if attempt.is_clean else f"{attempt.issue_count} issue(s)"
        )
        print(f"--- Attempt {attempt.attempt_number} ({label}): {status} ---")
        if not attempt.is_clean and not attempt.generation_failed:
            for issue in attempt.all_issues:
                print(f"  - {issue}")
        print()

    print("=" * 60)
    print(f"Converged: {result.converged}")
    print(f"Baseline issue count (attempt 0): {result.baseline_issue_count}")
    print(f"Final issue count: {result.final_issue_count}")
    print(f"Correction rounds used: {result.correction_rounds_used}")
    print("=" * 60)
    print()
    print("--- Final schema ---")
    print(json.dumps(result.final_schema.model_dump(), indent=2))


if __name__ == "__main__":
    main()
