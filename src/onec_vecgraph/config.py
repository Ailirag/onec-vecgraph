"""Application settings, loaded from environment / .env (pydantic-settings)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
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
    # Accept NEO4J_USERNAME too (orchestrator deployment skeletons use that spelling).
    neo4j_user: str = Field("neo4j", validation_alias=AliasChoices("neo4j_user", "neo4j_username"))
    neo4j_password: str = "onec_vecgraph_dev"
    neo4j_database: str = "neo4j"

    # ── Embeddings ─────────────────────────────────────────────────────
    # hashing | local | openai | voyage. Accept EMBEDDINGS_PROVIDER too (deployment-skeleton spelling).
    embedding_provider: str = Field(
        "hashing", validation_alias=AliasChoices("embedding_provider", "embeddings_provider"))
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
    # Accept ONEC_VECGRAPH_TENANT_ID too (orchestrator deployment skeleton spelling).
    default_tenant_id: str = Field(
        "default", validation_alias=AliasChoices("default_tenant_id", "onec_vecgraph_tenant_id"))
    default_config_id: str = "base"
    # Over HTTP, require the X-Tenant-Id header (no silent fallback to the shared
    # default — prevents cross-company data access). stdio (local dev) always uses defaults.
    require_tenant: bool = True

    # ── Shared / public corpora ────────────────────────────────────────
    # A reserved tenant holding PUBLIC, non-company-specific corpora (platform help, BSP/SSL
    # help, future public docs). Reads additively include it alongside the caller's tenant —
    # the agent sends only its own X-Tenant-Id; the shared tenant is appended server-side
    # (never client-controlled → no cross-tenant leakage). Public corpora are distinguished by
    # `source` (platform_help, bsp_help, ...). Ingest into it with `--tenant-id <shared_tenant_id>`.
    shared_tenant_id: str = "__shared__"
    include_shared_tenant: bool = True  # additively read the shared tenant in search/get_document

    # ── Development standards (1C v8std) read tools ────────────────────
    # `search_standards` / `get_standard` target the ITS development-standards corpus. It is ingested
    # (`type: its`) into the shared tenant with this corpus_version; ids look like '<prefix><number>'.
    standards_corpus_version: str = "platform:v8std"
    standards_id_prefix: str = "v8std_"

    def search_scope(self, tenant_id: str) -> list[str]:
        """Tenant ids a search/get_document call may read: caller + shared (if enabled, deduped)."""
        if self.include_shared_tenant and self.shared_tenant_id and self.shared_tenant_id != tenant_id:
            return [tenant_id, self.shared_tenant_id]
        return [tenant_id]

    # ── Auth (HTTP) ────────────────────────────────────────────────────
    # When enabled, every HTTP call must carry `Authorization: Bearer <token>`; the tenant
    # (and optional config) are derived from the token map below — NOT from a client-supplied
    # X-Tenant-Id (which can be spoofed). Default off → legacy trusted-header behaviour (dev/stdio).
    auth_enabled: bool = False
    # Token map: comma-separated "token=tenant" or "token=tenant:config" entries.
    # e.g. AUTH_TOKENS="tok_abc=acme,tok_xyz=globex:ext_crm"
    auth_tokens: str = ""

    # ── Overlay WRITE endpoint (separate, opt-in; the query server stays read-only) ────
    # A dedicated write-capable MCP server (cli `serve-write`) exposes only `index_overlay`,
    # confined to overlay tenants '<base>@task/*'. Off unless explicitly enabled.
    overlay_write_enabled: bool = False
    write_mcp_port: int = 8001
    # Token map authorizing overlay WRITE per base namespace: comma-separated "token=base".
    # A token may write only to '<base>@task/*'. e.g. WRITE_AUTH_TOKENS="wtok=grand-dev-mdm@release"
    write_auth_tokens: str = ""

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

    def write_auth_token_map(self) -> dict[str, str]:
        """Parse write_auth_tokens into {token: base_namespace}; a token may write '<base>@task/*'."""
        out: dict[str, str] = {}
        for entry in self.write_auth_tokens.split(","):
            entry = entry.strip()
            if not entry or "=" not in entry:
                continue
            token, _, base = entry.partition("=")
            token, base = token.strip(), base.strip()
            if token and base:
                out[token] = base
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
