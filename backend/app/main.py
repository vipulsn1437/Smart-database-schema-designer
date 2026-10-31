from fastapi import FastAPI

from app.api.routes import router as schema_router

app = FastAPI(
    title="Smart Database Schema Designer",
    description="AI-assisted database schema generation with normalization validation.",
    version="0.1.0",
)

app.include_router(schema_router)


@app.get("/health")
def health():
    return {"status": "ok"}
