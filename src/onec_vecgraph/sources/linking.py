"""Link doc units to configuration objects.

MENTIONS (high precision): explicit fqns provided by the adapter (e.g. ITS `related_fqns`) plus
`Kind.Name` fqns scanned out of the text, validated against existing Objects. Semantic RELATES_TO
(recall) is opt-in at ingest time (see ingest.link_semantic).
"""

from __future__ import annotations

import re

# Metadata kinds whose `Kind.Name` fqns we recognize in free text.
_KINDS = (
    "Catalog", "Document", "Enum", "InformationRegister", "AccumulationRegister",
    "AccountingRegister", "CalculationRegister", "ChartOfCharacteristicTypes", "ChartOfAccounts",
    "ChartOfCalculationTypes", "CommonModule", "Report", "DataProcessor", "Constant",
    "ExchangePlan", "BusinessProcess", "Task", "Subsystem", "Role", "DocumentJournal",
    "DefinedType", "CommonForm",
)
_FQN_RE = re.compile(r"\b(?:" + "|".join(_KINDS) + r")\.[A-Za-zА-Яа-яЁё0-9_]+")


def extract_fqn_mentions(*texts: str) -> set[str]:
    """`Kind.Name` fqns referenced in the given texts (e.g. 'Document.РеализацияТоваров')."""
    out: set[str] = set()
    for t in texts:
        if t:
            out.update(m.group(0) for m in _FQN_RE.finditer(t))
    return out


def link_mentions(store, tenant_id: str, units_by_fqn: list[tuple[str, object]]) -> int:
    """Create MENTIONS edges (doc owner → Object) for explicit + scanned fqns that actually exist.
    units_by_fqn: [(owner_fqn, DocUnit)]. Returns edges written."""
    per_doc: dict[str, set[str]] = {}
    candidates: set[str] = set()
    for doc_fqn, u in units_by_fqn:
        cands = set(getattr(u, "links", []) or []) | extract_fqn_mentions(u.text, u.title)
        if cands:
            per_doc[doc_fqn] = cands
            candidates |= cands
    if not candidates:
        return 0
    existing = store.existing_object_fqns(tenant_id, sorted(candidates))
    rows = [
        {"doc_fqn": doc_fqn, "object_fqn": o}
        for doc_fqn, cands in per_doc.items()
        for o in (cands & existing)
    ]
    return store.write_mentions(tenant_id, rows) if rows else 0
