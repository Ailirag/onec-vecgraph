import pytest

from onec_vecgraph.config import Settings
from onec_vecgraph.tenancy import TenantContext, TenantResolutionError, resolve


class _Req:
    def __init__(self, headers: dict):
        self.headers = headers


class _RC:
    def __init__(self, request):
        self.request = request


class _Ctx:
    def __init__(self, request, has_rc: bool = True):
        self._rc = _RC(request)
        self._has = has_rc

    @property
    def request_context(self):
        if not self._has:
            raise ValueError("Context is not available outside of a request")
        return self._rc


def test_resolve_from_headers() -> None:
    ctx = _Ctx(_Req({"X-Tenant-Id": "acme", "X-Config-Id": "ext:LLM"}))
    assert resolve(ctx, Settings()) == TenantContext("acme", "ext:LLM")


def test_missing_tenant_header_is_rejected_when_required() -> None:
    ctx = _Ctx(_Req({}))
    with pytest.raises(TenantResolutionError):
        resolve(ctx, Settings(require_tenant=True))


def test_missing_tenant_header_falls_back_when_not_required() -> None:
    ctx = _Ctx(_Req({}))
    assert resolve(ctx, Settings(require_tenant=False, default_tenant_id="d")).tenant_id == "d"


def test_stdio_uses_defaults() -> None:
    ctx = _Ctx(None, has_rc=False)  # no HTTP request context (stdio)
    assert resolve(ctx, Settings(default_tenant_id="dev")).tenant_id == "dev"
