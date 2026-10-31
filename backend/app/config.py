from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    groq_api_key: str = ""
    model_name: str = "llama-3.3-70b-versatile"
    max_tokens: int = 4000
    max_correction_attempts: int = 3

    # PostgreSQL connection string used by the deployment feature, e.g.
    # postgresql://postgres:password@localhost:5432/postgres
    # Optional: everything except /api/deploy works without it.
    database_url: str = ""

    # protected_namespaces=("settings_",) stops pydantic warning that the
    # `model_name` field clashes with its reserved "model_" prefix.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", protected_namespaces=("settings_",)
    )


settings = Settings()
