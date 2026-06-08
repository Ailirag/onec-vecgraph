import pytest

from onec_vecgraph.config import Settings
from onec_vecgraph.tenancy import TenantContext, TenantResolutionError, resolve


class _Ctx:
    """Minimal stand-in for an MCP Context carrying HTTP request headers."""
    def __init__(self, headers: dict[str, str]):
        req = type("Req", (), {"headers": headers})()
        rc = type("RC", (), {"request": req})()
        self.request_context = rc


def test_auth_token_map_parses_tenant_and_optional_config() -> None:
    s = Settings(auth_tokens="tok_a=acme, tok_b=globex:ext_crm , bad, =x, y=")
    m = s.auth_token_map()
    assert m == {"tok_a": ("acme", None), "tok_b": ("globex", "ext_crm")}


def test_resolve_legacy_trusted_header_when_auth_disabled() -> None:
    s = Settings(auth_enabled=False, require_tenant=True)
    ctx = _Ctx({"X-Tenant-Id": "acme", "X-Config-Id": "base"})
    assert resolve(ctx, s) == TenantContext("acme", "base")


def test_resolve_bearer_token_maps_to_tenant_and_ignores_spoofed_header() -> None:
    s = Settings(auth_enabled=True, auth_tokens="tok_a=acme:ext1")
    # client also sends a spoofed X-Tenant-Id — must be ignored in favour of the token map
    ctx = _Ctx({"Authorization": "Bearer tok_a", "X-Tenant-Id": "globex"})
    assert resolve(ctx, s) == TenantContext("acme", "ext1")


def test_resolve_bearer_config_header_used_when_token_has_no_pinned_config() -> None:
    s = Settings(auth_enabled=True, auth_tokens="tok_a=acme")
    ctx = _Ctx({"Authorization": "Bearer tok_a", "X-Config-Id": "ext_crm"})
    assert resolve(ctx, s) == TenantContext("acme", "ext_crm")


def test_resolve_rejects_missing_or_invalid_token_when_auth_enabled() -> None:
    s = Settings(auth_enabled=True, auth_tokens="tok_a=acme")
    with pytest.raises(TenantResolutionError):
        resolve(_Ctx({}), s)
    with pytest.raises(TenantResolutionError):
        resolve(_Ctx({"Authorization": "Bearer wrong"}), s)
