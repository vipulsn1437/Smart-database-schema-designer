"""
Quick manual test for the AI generation module, without needing to run the
FastAPI server. Usage:

    python -m scripts.try_generation "I need a system to track students, \
the courses they enroll in, and their grades"

Requires ANTHROPIC_API_KEY to be set (via .env or environment variable).
"""

import sys
import json
from app.services.ai_generation import SchemaGenerator, SchemaGenerationError


def main():
    if len(sys.argv) < 2:
        print('Usage: python -m scripts.try_generation "your requirements here"')
        sys.exit(1)

    requirements = sys.argv[1]
    generator = SchemaGenerator()

    print(f"Requirements: {requirements}\n")
    print("Calling model...\n")

    try:
        schema, raw = generator.generate(requirements)
    except SchemaGenerationError as e:
        print(f"Generation failed: {e}")
        sys.exit(1)

    print("--- Parsed schema ---")
    print(json.dumps(schema.model_dump(), indent=2))


if __name__ == "__main__":
    main()
