"""Tenant context.

Every node/chunk in Neo4j is scoped by (tenant_id, config_id). In HTTP mode the
context is resolved from the authenticated request (headers) rather than from tool
arguments, to prevent cross-tenant leakage. For M0 we fall back to configured
defaults; header extraction is wired in a later milestone.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings

TENANT_HEADER = "X-Tenant-Id"
CONFIG_HEADER = "X-Config-Id"


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    config_id: str

    def as_filter(self) -> dict[str, str]:
        """Parameters to inject into every Cypher query for isolation."""
        return {"tenant_id": self.tenant_id, "config_id": self.config_id}


def default_context(settings: Settings) -> TenantContext:
    return TenantContext(
        tenant_id=settings.default_tenant_id,
        config_id=settings.default_config_id,
    )


def context_from_headers(headers: dict[str, str], settings: Settings) -> TenantContext:
    """Resolve tenant context from request headers, falling back to defaults."""
    lower = {k.lower(): v for k, v in headers.items()}
    return TenantContext(
        tenant_id=lower.get(TENANT_HEADER.lower(), settings.default_tenant_id),
        config_id=lower.get(CONFIG_HEADER.lower(), settings.default_config_id),
    )


class TenantResolutionError(Exception):
    """Raised when a tenant cannot be resolved for an HTTP request (isolation guard)."""


def _http_request(ctx: object):
    """Return the Starlette Request for the current MCP call, or None (e.g. stdio)."""
    try:
        return ctx.request_context.request  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - no request context (stdio) or not available
        return None


def _bearer_token(headers: object) -> str | None:
    """Extract the token from an `Authorization: Bearer <token>` header (case-insensitive)."""
    auth = None
    for key in ("Authorization", "authorization"):
        try:
            auth = headers.get(key)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            auth = None
        if auth:
            break
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def resolve(ctx: object, settings: Settings) -> TenantContext:
    """Resolve the tenant/config for an MCP call.

    HTTP, auth_enabled: require `Authorization: Bearer <token>`; tenant/config come from the
    token map (a client cannot spoof tenant via X-Tenant-Id). X-Config-Id may still override
    config when the token doesn't pin one.
    HTTP, legacy (auth disabled): read X-Tenant-Id / X-Config-Id; if tenant absent and
    require_tenant is set, raise (no silent fallback -> no cross-company leakage).
    stdio (no HTTP request): use configured defaults.
    """
    request = _http_request(ctx)
    if request is not None and getattr(request, "headers", None) is not None:
        headers = request.headers

        if settings.auth_enabled:
            token = _bearer_token(headers)
            mapping = settings.auth_token_map()
            if not token or token not in mapping:
                raise TenantResolutionError(
                    "Missing or invalid 'Authorization: Bearer <token>' for this request."
                )
            tenant_id, pinned_config = mapping[token]
            return TenantContext(
                tenant_id=tenant_id,
                config_id=pinned_config or headers.get(CONFIG_HEADER) or settings.default_config_id,
            )

        tenant_id = headers.get(TENANT_HEADER)
        if not tenant_id:
            if settings.require_tenant:
                raise TenantResolutionError(
                    f"Missing required '{TENANT_HEADER}' header for this request."
                )
            tenant_id = settings.default_tenant_id
        return TenantContext(
            tenant_id=tenant_id,
            config_id=headers.get(CONFIG_HEADER) or settings.default_config_id,
        )
    return default_context(settings)


def _resolve_base(ctx: object, mapping: dict[str, str], what: str) -> str | None:
    """Resolve the authorized base namespace for a privileged call from a {token: base} map.

    Returns None when the map is empty (trusted/dev mode); raises if the map is configured but the
    request carries no matching bearer token. Shared by overlay-write and admin/baseline auth.
    """
    if not mapping:
        return None
    request = _http_request(ctx)
    headers = getattr(request, "headers", None) if request is not None else None
    token = _bearer_token(headers) if headers is not None else None
    if not token or token not in mapping:
        raise TenantResolutionError(f"Missing or invalid {what} 'Authorization: Bearer <token>'.")
    return mapping[token]


def resolve_write_base(ctx: object, settings: Settings) -> str | None:
    """Authorized base namespace for an overlay WRITE call (from the write bearer-token map).

    Returns the base tenant the token may write overlays under ('<base>@task/*'), or None when no
    write tokens are configured (trusted/dev mode — the caller still confines writes to an overlay
    tenant). Raises if write tokens ARE configured but the request carries no matching one.
    """
    return _resolve_base(ctx, settings.write_auth_token_map(), "write")


def resolve_admin_base(ctx: object, settings: Settings) -> str | None:
    """Authorized base namespace for an ADMIN/baseline-reindex call (from the admin bearer-token map).

    Returns the base tenant the token may baseline-reindex ('<base>' itself — owner-of-base), or None
    when no admin tokens are configured (trusted/dev mode). Raises if admin tokens ARE configured but
    the request carries no matching one. Unlike write tokens, this authorizes writing the baseline.
    """
    return _resolve_base(ctx, settings.admin_auth_token_map(), "admin")
