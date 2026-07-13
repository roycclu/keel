from keel.config import Settings


def test_from_env_loads_configured_dotenv(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=file-key\nKEEL_WEB_SEARCH_API_KEY=search-key\nKEEL_LLM_MODEL=test-model\n"
    )
    for name in ("OPENAI_API_KEY", "KEEL_WEB_SEARCH_API_KEY", "KEEL_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KEEL_ENV_FILE", str(env_file))

    settings = Settings.from_env()

    assert settings.llm_api_key == "file-key"
    assert settings.web_search_api_key == "search-key"
    assert settings.llm_model == "test-model"


def test_shell_environment_overrides_dotenv(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=file-key\n")
    monkeypatch.setenv("KEEL_ENV_FILE", str(env_file))
    monkeypatch.setenv("OPENAI_API_KEY", "shell-key")

    settings = Settings.from_env()

    assert settings.llm_api_key == "shell-key"


def test_provider_neutral_llm_settings_take_precedence(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "KEEL_LLM_API_KEY=keel-key\n"
        "OPENAI_API_KEY=openai-key\n"
        "KEEL_LLM_HTTP_REFERER=https://example.com\n"
        "KEEL_LLM_APP_TITLE=Example App\n"
    )
    for name in (
        "KEEL_LLM_API_KEY",
        "OPENAI_API_KEY",
        "KEEL_LLM_HTTP_REFERER",
        "KEEL_LLM_APP_TITLE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KEEL_ENV_FILE", str(env_file))

    settings = Settings.from_env()

    assert settings.llm_api_key == "keel-key"
    assert settings.llm_http_referer == "https://example.com"
    assert settings.llm_app_title == "Example App"


def test_from_env_loads_langfuse_settings(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "KEEL_OBSERVABILITY_BACKEND=langfuse\n"
        "LANGFUSE_PUBLIC_KEY=pk-test\n"
        "LANGFUSE_SECRET_KEY=sk-test\n"
        "LANGFUSE_BASE_URL=https://us.cloud.langfuse.com\n"
        "LANGFUSE_TRACING_ENVIRONMENT=test\n"
    )
    for name in (
        "KEEL_OBSERVABILITY_BACKEND",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
        "LANGFUSE_TRACING_ENVIRONMENT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KEEL_ENV_FILE", str(env_file))

    settings = Settings.from_env()

    assert settings.observability_backend == "langfuse"
    assert settings.langfuse_public_key == "pk-test"
    assert settings.langfuse_secret_key == "sk-test"
    assert settings.langfuse_base_url == "https://us.cloud.langfuse.com"
    assert settings.langfuse_environment == "test"


def test_from_env_loads_web_context_settings(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "KEEL_WEB_SEARCH_MODE=web\n"
        "KEEL_WEB_CONTEXT_MAX_URLS=12\n"
        "KEEL_WEB_CONTEXT_MAX_TOKENS=12000\n"
        "KEEL_WEB_CONTEXT_MAX_TOKENS_PER_URL=768\n"
        "KEEL_WEB_CONTEXT_MAX_SNIPPETS=24\n"
        "KEEL_WEB_CONTEXT_MAX_SNIPPETS_PER_URL=4\n"
        "KEEL_WEB_CONTEXT_THRESHOLD=balanced\n"
        "KEEL_WEB_FETCH_FALLBACK_MAX_URLS=2\n"
    )
    names = [line.split("=", 1)[0] for line in env_file.read_text().splitlines()]
    for name in names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KEEL_ENV_FILE", str(env_file))

    settings = Settings.from_env()

    assert settings.web_search_mode == "web"
    assert settings.web_context_max_urls == 12
    assert settings.web_context_max_tokens == 12_000
    assert settings.web_context_max_tokens_per_url == 768
    assert settings.web_context_max_snippets == 24
    assert settings.web_context_max_snippets_per_url == 4
    assert settings.web_context_threshold == "balanced"
    assert settings.web_fetch_fallback_max_urls == 2
