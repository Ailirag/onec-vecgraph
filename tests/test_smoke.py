import math

from onec_vecgraph import __version__
from onec_vecgraph.config import Settings
from onec_vecgraph.embeddings.hashing import HashingEmbeddingProvider
from onec_vecgraph.tenancy import TenantContext, context_from_headers


def test_version() -> None:
    assert __version__ == "0.1.0"


def test_hashing_embeddings_are_deterministic_and_normalized() -> None:
    provider = HashingEmbeddingProvider(dim=64)
    a = provider.embed(["Справочник Контрагенты ИНН"])[0]
    b = provider.embed(["Справочник Контрагенты ИНН"])[0]
    assert a == b
    assert len(a) == 64
    assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0, abs_tol=1e-6)


def test_tenant_context_from_headers_falls_back_to_defaults() -> None:
    settings = Settings(default_tenant_id="acme", default_config_id="base")
    ctx = context_from_headers({"X-Tenant-Id": "client42"}, settings)
    assert ctx == TenantContext(tenant_id="client42", config_id="base")
    assert ctx.as_filter() == {"tenant_id": "client42", "config_id": "base"}
