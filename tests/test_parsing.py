from pathlib import Path

import pytest
from lxml import etree

from onec_vecgraph.parsing import parse_config
from onec_vecgraph.parsing.types import parse_type

DUMP = Path(r"H:\1C\xml\LLM_Subsystem_test")

_TYPE_XML = """
<Type xmlns="http://v8.1c.ru/8.3/MDClasses"
      xmlns:v8="http://v8.1c.ru/8.1/data/core"
      xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config">
  <v8:Type>cfg:CatalogRef.AI_Провайдеры</v8:Type>
  <v8:Type>cfg:EnumRef.AI_ТипAPI</v8:Type>
  <v8:Type>xs:string</v8:Type>
</Type>
"""


def test_parse_type_distinguishes_references_from_primitives() -> None:
    desc = parse_type(etree.fromstring(_TYPE_XML))
    assert desc.is_composite
    refs = desc.references
    assert {(r.ref_kind, r.ref_name) for r in refs} == {
        ("Catalog", "AI_Провайдеры"),
        ("Enum", "AI_ТипAPI"),
    }
    # xs:string is primitive, not a reference
    assert sum(1 for r in desc.refs if r.category == "primitive") == 1


def test_configurator_field_reads_fill_checking() -> None:
    """Конфигураторный <FillChecking> внутри <Properties> реквизита → Field.fill_checking (issue #1)."""
    from onec_vecgraph.parsing.objects import _parse_field

    required = (
        '<Attribute xmlns="http://v8.1c.ru/8.3/MDClasses" uuid="u1">'
        "<Properties><Name>ИНН</Name>"
        "<FillChecking>ShowError</FillChecking></Properties></Attribute>"
    )
    fld = _parse_field(etree.fromstring(required), "Attribute", "Catalog.Контрагенты")
    assert fld.name == "ИНН" and fld.fill_checking == "ShowError"

    optional = (
        '<Attribute xmlns="http://v8.1c.ru/8.3/MDClasses" uuid="u2">'
        "<Properties><Name>Комментарий</Name></Properties></Attribute>"
    )
    fld2 = _parse_field(etree.fromstring(optional), "Attribute", "Catalog.Контрагенты")
    assert fld2.fill_checking == ""


def test_configurator_tabular_reads_fill_checking() -> None:
    """Конфигураторный <FillChecking> на самой ТЧ → TabularSection.fill_checking (ТЧ обязательна)."""
    from onec_vecgraph.parsing.objects import _parse_tabular

    xml = (
        '<TabularSection xmlns="http://v8.1c.ru/8.3/MDClasses" uuid="t1">'
        "<Properties><Name>Строки</Name>"
        "<FillChecking>ShowError</FillChecking></Properties></TabularSection>"
    )
    ts = _parse_tabular(etree.fromstring(xml), "Catalog.Контрагенты")
    assert ts.name == "Строки" and ts.fill_checking == "ShowError"


@pytest.mark.skipif(not DUMP.is_dir(), reason="sample dump not present on this machine")
def test_parse_sample_dump_finds_base_and_extension() -> None:
    parsed = parse_config(DUMP, tenant_id="test")
    config_ids = {p.config_id for p in parsed.parts}
    assert "base" in config_ids
    assert any(c.startswith("ext:") for c in config_ids)

    by_kind: dict[str, int] = {}
    for obj in parsed.objects:
        by_kind[obj.kind] = by_kind.get(obj.kind, 0) + 1
    # The sample contains catalogs, enums, common modules and an event subscription.
    assert by_kind.get("Catalog", 0) >= 5
    assert by_kind.get("Enum", 0) >= 5
    assert by_kind.get("CommonModule", 0) >= 5
    assert by_kind.get("EventSubscription", 0) >= 1

    # A reference-typed attribute must have been parsed.
    providers = next(o for o in parsed.objects if o.fqn == "Catalog.AI_Провайдеры")
    ref_fields = [f for f in providers.fields if any(t.category == "reference" for t in f.types)]
    assert ref_fields, "expected at least one reference-typed attribute on AI_Провайдеры"
