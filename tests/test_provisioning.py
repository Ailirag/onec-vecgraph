import pytest

from onec_vecgraph.admin_server import _should_issue
from onec_vecgraph.config import Settings
from onec_vecgraph.tenancy import (
    TenantContext,
    TenantResolutionError,
    resolve,
    resolve_admin_base,
    resolve_write_base,
)


class _Ctx:
    """Minimal stand-in for an MCP Context carrying HTTP request headers."""

    def __init__(self, headers: dict[str, str]):
        req = type("Req", (), {"headers": headers})()
        rc = type("RC", (), {"request": req})()
        self.request_context = rc


def _boom(_tok):  # a lookup that must NOT be called
    raise AssertionError("token_lookup should not have been consulted")


# ── read resolve: env wins, store fallback on miss ──────────────────────
def test_resolve_env_token_wins_store_not_consulted() -> None:
    s = Settings(auth_enabled=True, auth_tokens="tok_a=acme:ext1")
    ctx = _Ctx({"Authorization": "Bearer tok_a"})
    assert resolve(ctx, s, token_lookup=_boom) == TenantContext("acme", "ext1")


def test_resolve_store_fallback_on_env_miss() -> None:
    s = Settings(auth_enabled=True, auth_tokens="")
    ctx = _Ctx({"Authorization": "Bearer prov_tok"})
    look = lambda tok: {"tenant_id": "acme", "config_id": "ext1"} if tok == "prov_tok" else None
    assert resolve(ctx, s, token_lookup=look) == TenantContext("acme", "ext1")


def test_resolve_store_config_none_falls_back_to_header_then_default() -> None:
    s = Settings(auth_enabled=True)
    look = lambda tok: {"tenant_id": "acme", "config_id": None}
    ctx = _Ctx({"Authorization": "Bearer prov", "X-Config-Id": "ext_crm"})
    assert resolve(ctx, s, token_lookup=look) == TenantContext("acme", "ext_crm")
    ctx2 = _Ctx({"Authorization": "Bearer prov"})
    assert resolve(ctx2, s, token_lookup=look) == TenantContext("acme", "base")


def test_resolve_raises_when_store_miss_and_env_miss() -> None:
    s = Settings(auth_enabled=True)
    with pytest.raises(TenantResolutionError):
        resolve(_Ctx({"Authorization": "Bearer nope"}), s, token_lookup=lambda t: None)


def test_resolve_auth_disabled_ignores_lookup() -> None:
    s = Settings(auth_enabled=False, require_tenant=True)
    ctx = _Ctx({"X-Tenant-Id": "acme", "X-Config-Id": "base"})
    assert resolve(ctx, s, token_lookup=_boom) == TenantContext("acme", "base")


# ── write/admin base resolve: env wins, store gives self-scoped base ─────
def test_resolve_write_base_env_wins() -> None:
    s = Settings(write_auth_tokens="wtok=grand@release")
    ctx = _Ctx({"Authorization": "Bearer wtok"})
    assert resolve_write_base(ctx, s, token_lookup=_boom) == "grand@release"


def test_resolve_write_base_store_fallback_uses_tenant_as_base() -> None:
    s = Settings(write_auth_tokens="")  # no env write tokens
    ctx = _Ctx({"Authorization": "Bearer prov"})
    look = lambda tok: {"tenant_id": "acme", "config_id": None}
    assert resolve_write_base(ctx, s, token_lookup=look) == "acme"


def test_resolve_write_base_trusted_when_no_map_and_no_lookup() -> None:
    s = Settings(write_auth_tokens="")
    assert resolve_write_base(_Ctx({}), s) is None  # unchanged dev/trusted contract


def test_resolve_write_base_raises_when_lookup_present_but_miss() -> None:
    s = Settings(write_auth_tokens="")
    with pytest.raises(TenantResolutionError):
        resolve_write_base(_Ctx({"Authorization": "Bearer x"}), s, token_lookup=lambda t: None)


def test_resolve_admin_base_self_scoped_via_store() -> None:
    s = Settings(admin_auth_tokens="")
    ctx = _Ctx({"Authorization": "Bearer prov"})
    look = lambda tok: {"tenant_id": "acme", "config_id": None}
    assert resolve_admin_base(ctx, s, token_lookup=look) == "acme"


def test_resolve_admin_base_env_only_when_no_lookup() -> None:
    # provisioning tools call resolve_admin_base WITHOUT a lookup → provisioned tokens can't provision
    s = Settings(admin_auth_tokens="atok=grand@release")
    with pytest.raises(TenantResolutionError):
        resolve_admin_base(_Ctx({"Authorization": "Bearer prov"}), s)  # not in env map, no lookup
    assert resolve_admin_base(_Ctx({"Authorization": "Bearer atok"}), s) == "grand@release"


# ── issue policy (strict: created or rotate) ────────────────────────────
@pytest.mark.parametrize(
    "exists,rotate,expected",
    [(False, False, True), (False, True, True), (True, False, False), (True, True, True)],
)
def test_should_issue(exists: bool, rotate: bool, expected: bool) -> None:
    assert _should_issue(exists, rotate) is expected
