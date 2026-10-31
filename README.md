# Smart Database Schema Designer

AI-assisted database schema generation with a self-implemented, deterministic
normalization validation engine.

## Current status

This scaffold implements **Module 2 (shared JSON schema model)** and
**Module 3 (AI schema generation)** from the project methodology, fully
tested and working end-to-end.

| Module | Status |
|---|---|
| 1. Requirement analysis | Done (literature review) |
| 2. Shared JSON schema data model | ✅ Implemented — `app/models/schema_models.py` |
| 3. AI schema generation module | ✅ Implemented — `app/services/ai_generation.py` |
| 4. Structural validation module | Not started |
| 5. Normalization validation engine | Not started (core contribution — next priority) |
| 6. Bounded correction loop | Not started (depends on 4 & 5) |
| 7. ER diagram visualization | Not started |
| 8. Conversational refinement | Not started |
| 9. Live deployment + NL-to-SQL + sample data | Not started |
| 10. Multi-domain testing & evaluation | Not started |

## Project structure

```
backend/
  app/
    main.py               # FastAPI entrypoint
    config.py              # Settings (reads .env)
    models/
      schema_models.py     # Shared Pydantic schema model (Module 2)
    services/
      prompts.py           # Prompt templates for generation & correction
      ai_generation.py     # AI schema generation module (Module 3)
    api/
      routes.py            # POST /api/generate
  scripts/
    try_generation.py      # Manual CLI test (no server needed)
  tests/
    test_schema_models.py           # Unit tests, no network required
    test_ai_generation_parsing.py   # Unit tests, no network required
  requirements.txt
  .env.example
```

## Setup

```bash
cd backend
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # then edit .env and add your GROQ_API_KEY
```

Get a **free** API key from https://console.groq.com/keys (Groq has a generous
free tier for open-source models like Llama 3.3 70B — no billing setup needed
to get started).

## Running the tests

The unit tests do **not** call the real LLM (the network call is mocked /
tested in isolation), so they run instantly and don't need an API key:

```bash
cd backend
python3 -m pytest tests/ -v
```

## Trying generation manually (needs a real API key)

```bash
cd backend
python -m scripts.try_generation "I need a system to track students, the courses they enroll in, and their grades"
```

This prints the parsed `CandidateSchema` as JSON — inspect it by hand and
check whether the functional dependencies and table structure look right
for a few different requirement descriptions before building the
normalization engine on top of it (Module 5 depends entirely on the AI
correctly inferring functional dependencies here, so it's worth spending
some time now looking at real outputs across a few domains).

## Running the API server

```bash
cd backend
uvicorn app.main:app --reload
```

Then visit `http://127.0.0.1:8000/docs` for interactive API docs, or:

```bash
curl -X POST http://127.0.0.1:8000/api/generate \
  -H "Content-Type: application/json" \
  -d '{"requirements": "I need a system to track students, the courses they enroll in, and their grades"}'
```

## Design notes (why it's built this way)

- **`CandidateSchema` is the single shared data structure** every module
  reads and writes (per Module 2 of the methodology). Structural validation,
  normalization validation, and the correction loop will all operate on this
  same object rather than inventing their own formats.
- **The AI generation module does not validate its own output.** This is
  deliberate — validation is Modules 4 and 5's job, implemented independently
  of the LLM, so the system's correctness guarantee doesn't depend on the
  LLM judging itself (the self-confirmation bias problem discussed in the
  literature review).
- **Functional dependencies are inferred by the LLM at generation time**,
  not computed from data. This is a modeling choice worth being explicit
  about in the paper: it means the normalization engine's output is only as
  good as the LLM's understanding of the requirements, which is exactly why
  Module 10's evaluation (comparing against hand-designed ground-truth
  schemas) matters.

## Next step

Build **Module 4 (structural validation)**: given a `CandidateSchema`, check
for duplicate table/column names, missing primary keys, and foreign keys
that reference non-existent tables/columns. This is a good next step because
it's simple, self-contained, and doesn't depend on functional dependencies
at all — unlike Module 5, which does.
