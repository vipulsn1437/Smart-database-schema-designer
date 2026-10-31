# Smart Database Schema Designer

**Intelligent database schema generation using AI and automated normalization.**

Describe your application in plain English. An LLM drafts a relational schema, and a self-implemented, rule-based engine checks it against database theory (1NF, 2NF, 3NF and BCNF). Violations are explained and sent back to the LLM for correction, up to three times. Validated schemas can be exported as SQL and deployed into a sandboxed PostgreSQL database.

> **The AI proposes. An algorithm verifies.** The correctness of the output does not depend on trusting the language model.

---

## Table of Contents

- [Why this project](#why-this-project)
- [How it works](#how-it-works)
- [Features](#features)
- [The normalization engine](#the-normalization-engine)
- [Tech stack](#tech-stack)
- [Getting started](#getting-started)
- [API](#api)
- [Example](#example)
- [Evaluation](#evaluation)
- [Testing](#testing)
- [Limitations](#limitations)
- [Roadmap](#roadmap)
- [Project information](#project-information)

---

## Why this project

A badly normalized schema does not fail on day one. It fails later, through update, insertion and deletion anomalies. If a clinic stores each therapist's branch on every appointment row, one partial update leaves the data contradicting itself.

LLMs can draft a schema in seconds, but the draft only looks plausible. Nothing proves it is correct, and a model asked to review its own work tends to agree with itself. This project separates the two jobs:

- The **LLM** does what it is good at: understanding a plain-English description.
- A **deterministic verifier** does what must be provable: checking the schema against formal relational theory.

---

## How it works

```
 Plain-English requirements
            |
            v
 +-----------------------+
 |  1. AI generation     |  LLM drafts tables, keys and functional
 |     (Groq, LLM)       |  dependencies as typed JSON
 +-----------+-----------+
             |
             v
 +-----------------------+
 |  2. Structural check  |  duplicate names, missing primary keys,
 |     (no LLM)          |  foreign keys to tables that don't exist
 +-----------+-----------+
             |
             v
 +-----------------------+
 |  3. Normalization     |  attribute closure, candidate keys,
 |     check (no LLM)    |  1NF / 2NF / 3NF / BCNF
 +-----------+-----------+
             |
     issues? |  yes: explain the issues and send them back
             |       to the LLM (maximum 3 attempts)
             |  no:  schema is converged
             v
 +-----------------------+
 |  4. Export & deploy   |  SQL DDL, sandboxed PostgreSQL
 +-----------------------+
```

If the schema still has issues after three attempts, it is reported as **unresolved** together with the remaining issues. It is never passed off as valid.

Steps 2 and 3 never call the model, so a verdict can be reproduced by hand from the schema alone.

---

## Features

**Implemented**

- Natural language to schema generation
- Structural validation (duplicate names, missing primary keys, dangling foreign keys)
- Normalization validation for 1NF, 2NF, 3NF and BCNF, with explained issues
- Bounded correction loop (maximum 3 attempts) that enforces decomposition instead of deletion
- SQL DDL export
- Live deployment into an isolated PostgreSQL sandbox, with dry runs and removal
- Evaluation harness comparing the pipeline with unguided LLM self-critique

**Designed, not yet part of the evaluated build**

- Sample data generation
- Natural-language querying of a deployed schema
- Interactive ER diagram with per-table explanations
- Conversational refinement of an existing schema
- Explain-existing-schema mode

---

## The normalization engine

The engine is implemented from first principles, without an external normalization library.

1. **Attribute closure.** Starting from an attribute set X, repeatedly add the right-hand side of any declared dependency whose left-hand side is already contained in the result, until nothing changes.
2. **Candidate keys.** Try attribute subsets from smallest to largest. A subset whose closure is every attribute, and which contains no smaller key, is a candidate key.
3. **Normal-form checks.**

| Normal form | Violation the engine reports |
| --- | --- |
| 1NF | A column holds several values in one field (for example a delimited list) |
| 2NF | A non-key attribute depends on only part of a composite candidate key |
| 3NF | A non-key attribute depends on something that is not a superkey |
| BCNF | A non-trivial dependency `X -> Y` where `X` is not a superkey |

Every issue records the table, the rule and the dependency responsible, so the same explanation is shown to a person and sent to the model.

Because BCNF is stricter than 3NF, one design flaw can be reported under both rules. Violation counts are therefore rule-level detections, not separate flaws.

---

## Tech stack

| Layer | Technology |
| --- | --- |
| Language | Python |
| API | FastAPI |
| Data models and settings | Pydantic |
| LLM | Groq API, `openai/gpt-oss-120b` |
| Database | PostgreSQL 16 (Docker), `psycopg` driver |
| Testing | pytest with the FastAPI test client |

---

## Getting started

### Prerequisites

- Python 3.10 or newer
- Docker (for the PostgreSQL server)
- A Groq API key

### Install

```bash
git clone <your-repo-url>
cd smart-schema-designer/backend

python -m venv venv
# Windows (PowerShell): .\venv\Scripts\Activate.ps1
# macOS / Linux:        source venv/bin/activate

pip install -r requirements.txt
```

### Configure

Create a `.env` file in `backend/`:

```env
GROQ_API_KEY=your_groq_api_key
# Add your PostgreSQL connection settings here as well
```

### Start PostgreSQL

```bash
docker run --name schema-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 -d postgres:16
```

### Run the API

```bash
uvicorn app.main:app --reload
```

Adjust the module path to match your entry point. FastAPI serves interactive documentation at `http://localhost:8000/docs`.

---

## API

| Route | Purpose |
| --- | --- |
| `/validate` | Check a schema for structural and normalization issues |
| `/ddl` | Export a validated schema as SQL DDL |
| `/design` | Generate a schema from a requirement and run the correction loop |
| `/deploy` | Deploy a validated schema into a sandbox |

### Deployment behaviour

| Request | Effect |
| --- | --- |
| `POST /api/deploy?keep=false` | **Dry run.** Executes the DDL on the live server to prove it works, then leaves nothing behind. |
| `POST /api/deploy?keep=true` | Deploys and keeps the schema in its own `sandbox_...` PostgreSQL schema. |
| `DELETE /api/deploy/sandbox_<id>` | Removes one kept sandbox. |

A request body that is not valid JSON, or does not match the schema model, is refused with HTTP `422` before anything is executed.

---

## Example

**Requirement**

> I need a database for a sports clinic to manage therapist appointments. Each session has a Patient, a Time slot and an assigned Therapist. A therapist only works at one Clinic Branch, but a branch has multiple therapists. Design a single table for these sessions.

**Raw draft.** One flat `sessions` table. The declared dependency `therapist_id -> clinic_branch_id` means the branch is repeated on every session. The engine reports a 3NF and a BCNF violation, and the structural check reports that the foreign keys point at tables that do not exist.

**After one correction round.** The branch moves into a `therapists` table, `patients` and `clinic_branches` tables are created, and `sessions` keeps only the keys and the time slot. Zero issues remain, and the schema deploys as four tables in PostgreSQL.

**Schema format.** Every module exchanges one typed JSON structure:

```json
{
  "name": "sessions",
  "columns": [
    {"name": "session_id", "type": "INTEGER", "nullable": false, "primary_key": true},
    {"name": "patient_id", "type": "INTEGER", "nullable": false},
    {"name": "therapist_id", "type": "INTEGER", "nullable": false},
    {"name": "clinic_branch_id", "type": "INTEGER", "nullable": false},
    {"name": "time_slot", "type": "TIMESTAMP", "nullable": false}
  ],
  "functional_dependencies": [
    {"determinant": ["session_id"], "dependent": ["patient_id", "therapist_id", "clinic_branch_id", "time_slot"]},
    {"determinant": ["therapist_id"], "dependent": ["clinic_branch_id"]}
  ]
}
```

---

## Evaluation

**Benchmark.** 25 author-written, plain-English requirement descriptions across independent domains (clinics, project costing, real estate, rentals, logistics, e-commerce and others). Each targets a specific normalization problem. One is deliberately out of scope (a 4NF multi-valued dependency) to test the boundary of the engine.

**Conditions.**

1. **Proposed pipeline:** generate, verify, correct (up to 3 attempts).
2. **LLM self-critique:** the same model reviews and corrects its own schema in one pass, with no hints about what is wrong.

The same independent verifier grades both conditions.

**Results (reported run).**

| Metric | Value |
| --- | --- |
| Domains with a flaw in the raw draft | 20 of 25 |
| Violations detected in raw drafts | 65 (32 3NF, 25 BCNF, 7 2NF, 1 1NF) |
| Domains resolved by the pipeline within 3 attempts | all 25 |
| Average correction rounds (domains needing correction) | 1.05 |

**Self-critique.** It resolved most tested domains. In the latest trial it resolved 17 of the 18 domains that had a baseline violation. It did not reliably resolve the hardest case, `project_assignment_costs`, which has two independent partial dependencies on a three-attribute composite key. In the failing trials it decomposed the tables correctly but left foreign keys pointing at tables it never created, a referential-integrity error that the structural check catches.

**Reproduce the self-critique experiment.**

```bash
python -m scripts.run_self_critique_experiment --fresh
```

The `--fresh` flag backs up any existing results to a timestamped file first. Results are written to `self_critique_results/` as `self_critique_results.json` and `self_critique_summary.md`.

**Read these numbers with care.** One model, author-written domains and a small number of trials. Each run regenerates the baseline schemas, so trials are not paired. Self-critique had one pass and the pipeline had up to three attempts.

---

## Testing

```bash
pytest
```

The backend has 102 automated tests. Most run without calling the model. Five run against a real PostgreSQL server in Docker, covering dry runs, kept deployments and removal. The tests cover closure and candidate keys against hand-derived results, each normal-form check, structural validation, model-output parsing, the correction loop with a scripted stand-in for the model, and the deployment API.

---

## Limitations

- The verifier only checks the dependencies the AI declares. If the model misses a dependency, a violation can pass undetected.
- The engine stops at BCNF. Multi-valued and join dependencies (4NF and 5NF) are not detected.
- Candidate-key enumeration is exponential in the number of columns in the worst case. This was not a problem for the table sizes evaluated.
- Evaluation used one model, 25 author-written domains and a small number of trials.

---

## Roadmap

- Paired evaluation (one saved baseline per domain, both conditions run on it)
- Repeated trials of both conditions, plain non-adversarial prompts and a second model
- 4NF and 5NF detection
- Natural-language querying, ER diagram generation and conversational refinement
- API authentication and automatic expiry of kept sandboxes

---

## Project information

**Paper:** *Smart Database Schema Designer: Intelligent Database Schema Generation Using Artificial Intelligence and Automated Normalization*, presented at ICETMOMTM 2026 (Paper ID ETMOS26_106), Amity University Uttar Pradesh, 27 October 2026.

**Authors:** Vipul Negi, Abhay Baliyan and Dr. Roshan Lal, B.Tech CSE, Amity School of Engineering and Technology, Amity University Uttar Pradesh, India.