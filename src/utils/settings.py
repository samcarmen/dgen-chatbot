from pydantic import ConfigDict
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = ConfigDict(
        case_sensitive=True,
        env_file=".env",
        extra="ignore",
    )  # pyright: ignore

    VERSION: str = "v1"
    ENVIRONMENT: str = "dev"

    GCP_PROJECT_ID: str = "dgen-chatbot"
    AGENT_ENGINE_REGION: str = "us-central1"
    AGENT_ENGINE_BUCKET: str = "dgen-agent-engine"
    AGENT_ENGINE_RESOURCE_NUM: str

    FIRESTORE_DATABASE: str = "chatbot-memory"
    FIRESTORE_COLLECTION: str = "widget_sessions"

    TEMPERATURE: float = 0.2
    TOP_P: float = 0.9
    TOP_K: int = 40
    MAX_OUTPUT_TOKENS: int = 512
    MAX_MESSAGE_LENGTH: int = 2000

    # HTTP security
    ALLOWED_ORIGINS: str
    BLOCKED_PROMPT_PATTERNS: str = (
        r"(?i)(ignore previous|system prompt|instructions above|"
        r"disregard|override|pretend to be|act as system|jailbreak)"
    )
    RATE_LIMIT_COLLECTION: str = "widget_rate_limits"
    RATE_LIMIT_MAX_REQUESTS: int = 30
    RATE_LIMIT_WINDOW_SECONDS: int = 60
