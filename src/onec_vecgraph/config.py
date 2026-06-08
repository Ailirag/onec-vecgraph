"""Application settings, loaded from environment / .env (pydantic-settings)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Neo4j (graph + vectors) ────────────────────────────────────────
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "onec_vecgraph_dev"
    neo4j_database: str = "neo4j"

    # ── Embeddings ─────────────────────────────────────────────────────
    # hashing | local | openai | voyage
    embedding_provider: str = "hashing"
    # Per 1C-RAG best practice (documents1c / metacode both use Qwen3-Embedding).
    # 0.6B (1024-dim) is the responsive default; switch to Qwen/Qwen3-Embedding-4B
    # (2560-dim) for higher quality. BGE-m3 is a multilingual alternative.
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    embedding_device: str = "auto"  # auto | cuda | cpu
    embedding_query_prompt: str = "query"  # sentence-transformers prompt for query-side
    embedding_batch_size: int = 16
    embedding_max_seq_length: int = 256  # cap tokens/chunk (bounds VRAM on long cards)
    embedding_dim: int = 256  # only used by the hashing provider
    # Cloud only: override output dimensions (OpenAI text-embedding-3-*, Voyage voyage-3-large/3.5);
    # None = model default. Must be ≤ 4096 (Neo4j vector index limit).
    embedding_dimensions: int | None = None
    vector_overfetch: int = 5  # over-fetch factor before tenant filtering
    openai_api_key: str | None = None
    openai_base_url: str | None = None  # for OpenAI-compatible gateways (Azure/proxy/self-hosted)
    voyage_api_key: str | None = None

    # Optional cross-encoder reranker (off by default; downloads a ~2GB model).
    rerank_enabled: bool = False
    rerank_model: str = "BAAI/bge-reranker-v2-m3"

    # ── MCP server ─────────────────────────────────────────────────────
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000
    mcp_path: str = "/mcp"

    # ── Tenant context ─────────────────────────────────────────────────
    default_tenant_id: str = "default"
    default_config_id: str = "base"
    # Over HTTP, require the X-Tenant-Id header (no silent fallback to the shared
    # default — prevents cross-company data access). stdio (local dev) always uses defaults.
    require_tenant: bool = True

    # ── Auth (HTTP) ────────────────────────────────────────────────────
    # When enabled, every HTTP call must carry `Authorization: Bearer <token>`; the tenant
    # (and optional config) are derived from the token map below — NOT from a client-supplied
    # X-Tenant-Id (which can be spoofed). Default off → legacy trusted-header behaviour (dev/stdio).
    auth_enabled: bool = False
    # Token map: comma-separated "token=tenant" or "token=tenant:config" entries.
    # e.g. AUTH_TOKENS="tok_abc=acme,tok_xyz=globex:ext_crm"
    auth_tokens: str = ""

    # Empty env strings (e.g. compose `VAR: "${VAR:-}"`) → None for optional fields, so an
    # unset cloud var doesn't break parsing (e.g. "" is not a valid int for embedding_dimensions).
    @field_validator(
        "embedding_dimensions", "openai_api_key", "openai_base_url", "voyage_api_key",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, v: object) -> object:
        return None if isinstance(v, str) and v.strip() == "" else v

    def auth_token_map(self) -> dict[str, tuple[str, str | None]]:
        """Parse auth_tokens into {token: (tenant_id, config_id|None)}."""
        out: dict[str, tuple[str, str | None]] = {}
        for entry in self.auth_tokens.split(","):
            entry = entry.strip()
            if not entry or "=" not in entry:
                continue
            token, _, target = entry.partition("=")
            token, target = token.strip(), target.strip()
            if not token or not target:
                continue
            tenant, sep, config = target.partition(":")
            out[token] = (tenant.strip(), config.strip() if sep and config.strip() else None)
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
