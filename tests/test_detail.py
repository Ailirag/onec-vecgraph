from lxml import etree

from onec_vecgraph.parsing import objects
from onec_vecgraph.parsing.objects import _flatten_properties

_PROPS_XML = """
<Properties xmlns="http://v8.1c.ru/8.3/MDClasses"
            xmlns:v8="http://v8.1c.ru/8.1/data/core">
  <Name>Контрагенты</Name>
  <Hierarchical>true</Hierarchical>
  <CodeLength>9</CodeLength>
  <FullTextSearch>Use</FullTextSearch>
  <Empty></Empty>
  <Synonym>
    <v8:item><v8:lang>ru</v8:lang><v8:content>Контрагенты</v8:content></v8:item>
  </Synonym>
</Properties>
"""


def _props():
    return etree.fromstring(_PROPS_XML)


def test_flatten_scalars_kept_and_empty_dropped() -> None:
    d = _flatten_properties(_props())
    assert d["Hierarchical"] == "true"
    assert d["CodeLength"] == "9"
    assert d["FullTextSearch"] == "Use"
    assert d["Name"] == "Контрагенты"
    assert "Empty" not in d  # empty scalar dropped


def test_flatten_structured_kept_as_raw_inner_xml() -> None:
    d = _flatten_properties(_props())
    # structured property -> faithful raw inner-XML fallback
    assert "Synonym" in d
    assert "<" in d["Synonym"] and "Контрагенты" in d["Synonym"]


def test_flatten_truncates_large_structured_value() -> None:
    big = "<Properties xmlns='http://v8.1c.ru/8.3/MDClasses'><Blob>" + \
          "".join(f"<i>{n}</i>" for n in range(2000)) + "</Blob></Properties>"
    d = _flatten_properties(etree.fromstring(big))
    assert d["Blob"].endswith("…(truncated)")
    assert len(d["Blob"]) <= objects._RAW_MAX + len("…(truncated)")


def test_flatten_none_returns_empty() -> None:
    assert _flatten_properties(None) == {}
