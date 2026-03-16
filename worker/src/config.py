"""Configuration loaded from environment variables.

Uses Pydantic BaseSettings for validation and type coercion.
Explicit dependency injection — no global state.
Supports both OpenAI (default) and Ollama as LLM backends.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings


class WorkerConfig(BaseSettings):
    """Worker service configuration.

    All values sourced from env vars with BULLETFARM_ prefix.
    Example: BULLETFARM_GITHUB_TOKEN sets github_token.
    """

    github_token: str = ""
    elasticsearch_url: str = "http://elasticsearch-master:9200"
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o-mini"
    openai_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"
    worker_port: int = 8000

    model_config = {"env_prefix": "BULLETFARM_"}
