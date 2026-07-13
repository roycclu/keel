"""Runtime configuration, read once from the environment.

Kept dependency-light (no pydantic-settings): a plain model plus `from_env`. Defaults
target the OpenAI API for reasoning and the test.wikipedia.org write path (never
production in Phase 1).
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel
from dotenv import load_dotenv


class Settings(BaseModel):
    # --- LLM (provider-agnostic; see keel/llm) ---
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-5.6-luna"
    llm_api_key: str | None = None
    llm_http_referer: str | None = None
    llm_app_title: str | None = None
    llm_timeout_s: float = 120.0

    # --- Wikipedia (Phase 1 writes go to the test wiki only) ---
    wiki_api_base: str = "https://test.wikipedia.org/w/api.php"
    wiki_oauth_token: str | None = None  # required only when actually submitting
    user_agent: str = "Keel/0.1 (human-supervised; contact roycclu@gmail.com)"

    # --- web research ---
    web_search_provider: str = "brave"  # adapter key; see tools/web.py
    web_search_api_key: str | None = None

    # --- execution ---
    http_timeout_s: float = 30.0
    sqlite_path: str = "keel.db"
    per_run_token_budget: int = 200_000  # hard ceiling enforced by the runbook loop
    dry_run_submit: bool = False  # when true, submit renders + logs the diff, posts nothing

    # --- observability ---
    observability_backend: Literal["jsonl", "langfuse"] = "jsonl"
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_environment: str = "development"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(os.environ.get("KEEL_ENV_FILE", ".env"), override=False)
        env = os.environ
        return cls(
            llm_base_url=env.get("KEEL_LLM_BASE_URL", cls.model_fields["llm_base_url"].default),
            llm_model=env.get("KEEL_LLM_MODEL", cls.model_fields["llm_model"].default),
            llm_api_key=env.get("KEEL_LLM_API_KEY") or env.get("OPENAI_API_KEY"),
            llm_http_referer=env.get("KEEL_LLM_HTTP_REFERER"),
            llm_app_title=env.get("KEEL_LLM_APP_TITLE"),
            wiki_api_base=env.get("KEEL_WIKI_API_BASE", cls.model_fields["wiki_api_base"].default),
            wiki_oauth_token=env.get("KEEL_WIKI_OAUTH_TOKEN"),
            web_search_provider=env.get(
                "KEEL_WEB_SEARCH_PROVIDER", cls.model_fields["web_search_provider"].default
            ),
            web_search_api_key=env.get("KEEL_WEB_SEARCH_API_KEY"),
            sqlite_path=env.get("KEEL_SQLITE_PATH", cls.model_fields["sqlite_path"].default),
            observability_backend=env.get(
                "KEEL_OBSERVABILITY_BACKEND",
                cls.model_fields["observability_backend"].default,
            ),
            langfuse_public_key=env.get("LANGFUSE_PUBLIC_KEY"),
            langfuse_secret_key=env.get("LANGFUSE_SECRET_KEY"),
            langfuse_base_url=env.get(
                "LANGFUSE_BASE_URL", cls.model_fields["langfuse_base_url"].default
            ),
            langfuse_environment=env.get(
                "LANGFUSE_TRACING_ENVIRONMENT",
                cls.model_fields["langfuse_environment"].default,
            ),
        )
