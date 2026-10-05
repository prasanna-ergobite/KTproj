from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application Settings for AutoKT.
    Reads environment variables from environment or .env file.
    """
    # LLM Provider Configuration ("stub" | "gemini" | "azure" | "azure_foundry")
    LLM_PROVIDER: str = "azure_foundry"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "gpt-5-mini"
    LLM_BASE_URL: str = ""
    AZURE_OPENAI_ENDPOINT: str = ""
    AZURE_OPENAI_DEPLOYMENT: str = ""
    AZURE_OPENAI_API_VERSION: str = ""

    def get_effective_llm_provider(self) -> str:
        prov = (self.LLM_PROVIDER or "").lower().strip()
        if prov in ("", "none", "null"):
            if self.LLM_BASE_URL and self.LLM_API_KEY:
                return "azure_foundry"
            return "stub"
        return prov

    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "password123"  # DEV DEFAULT — override via NEO4J_PASSWORD in .env for production
    CHROMA_HOST: str = "localhost"
    CHROMA_PORT: int = 8000
    EMBEDDING_PROVIDER: str = "nomic"  # "nomic" = nomic-ai/nomic-embed-text-v1.5 via sentence-transformers
    REPO_LOCAL_ALLOWED_ROOT: str = ""

    # Cross-Encoder Reranking (Milestone 15 — final retrieval precision stage)
    # RERANKER_PROVIDER options:
    #   "cross-encoder" — loads ms-marco-MiniLM-L-6-v2 via sentence_transformers.CrossEncoder
    #   "disabled"      — skips reranking entirely; rrf_score remains the primary sort key
    #   "stub"          — returns deterministic dummy descending scores (for test isolation; no model load)
    RERANKER_PROVIDER: str = "cross-encoder"
    RERANKER_MODEL_NAME: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    RERANKER_TOP_K: int = 30
    RERANKER_HEAD_CHARS: int = 250
    RERANKER_TAIL_CHARS: int = 150
    # Business-to-Code Mapping (Milestone 20)
    BUSINESS_MAPPING_RERANK_THRESHOLD: float = 0.5
    BUSINESS_MAPPING_MAX_CANDIDATES: int = 20
    BUSINESS_MAPPING_LLM_MAX_CANDIDATES: int = 5

    JWT_SECRET: str = "super_secret_jwt_key_change_in_production_123456"  # DEV DEFAULT — override via JWT_SECRET in .env for production
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # PostgreSQL Configuration
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"  # DEV DEFAULT — override via POSTGRES_PASSWORD in .env for production
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "autokt"
    DATABASE_URL: str | None = None

    @property
    def database_url_sync(self) -> str:
        """
        Precedence: If DATABASE_URL is set and non-empty, use it directly.
        Otherwise, construct the synchronous PostgreSQL connection URL from
        individual POSTGRES_* environment variables.
        """
        if self.DATABASE_URL and self.DATABASE_URL.strip():
            return self.DATABASE_URL.strip()
        return f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
