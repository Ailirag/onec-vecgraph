"""CommonForm parsing: a common form is a standalone managed form (no owning object).

It has no <Form> child in its metadata XML; its layout/module live directly in the
object's own dir (CommonForms/<Name>/Ext/...). The parser must synthesize a self-form so
the form pipeline (text / handlers / code chunks / callgraph / HANDLES) picks it up.
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from onec_vecgraph import chunking
from onec_vecgraph.bsl.parser import parse_module
from onec_vecgraph.parsing.forms import extract_form_text, parse_form_handlers
from onec_vecgraph.parsing.ns import first_child_element
from onec_vecgraph.parsing.objects import parse_object

_COMMON_FORM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">
  <CommonForm uuid="11111111-1111-1111-1111-111111111111">
    <Properties>
      <Name>ТестОбщаяФорма</Name>
      <Synonym>
        <v8:item><v8:lang>ru</v8:lang><v8:content>Тестовая общая форма</v8:content></v8:item>
      </Synonym>
      <FormType>Managed</FormType>
    </Properties>
  </CommonForm>
</MetaDataObject>
"""

_FORM_LAYOUT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" xmlns:v8="http://v8.1c.ru/8.1/data/core">
  <Title>
    <v8:item><v8:lang>ru</v8:lang><v8:content>Заголовок тестовой формы</v8:content></v8:item>
  </Title>
  <Events>
    <Event name="OnOpen">ПриОткрытии</Event>
  </Events>
  <ChildItems>
    <InputField name="Поле1">
      <Events>
        <Event name="OnChange">Поле1ПриИзменении</Event>
      </Events>
    </InputField>
  </ChildItems>
</Form>
"""

_FORM_MODULE_BSL = """&НаКлиенте
Процедура ПриОткрытии(Отказ)
    Сообщить("Открытие тестовой общей формы");
    ОбновитьОтображениеДанных();
КонецПроцедуры

&НаКлиенте
Процедура Поле1ПриИзменении(Элемент)
    Сообщить("Поле изменено");
КонецПроцедуры
"""


def _make_common_form(root: Path) -> Path:
    """Lay out CommonForms/ТестОбщаяФорма{,.xml} with Ext/Form.xml + Ext/Form/Module.bsl."""
    cf_dir = root / "CommonForms"
    obj_dir = cf_dir / "ТестОбщаяФорма"
    (obj_dir / "Ext" / "Form").mkdir(parents=True)
    (cf_dir / "ТестОбщаяФорма.xml").write_text(_COMMON_FORM_XML, encoding="utf-8")
    (obj_dir / "Ext" / "Form.xml").write_text(_FORM_LAYOUT_XML, encoding="utf-8")
    (obj_dir / "Ext" / "Form" / "Module.bsl").write_text(_FORM_MODULE_BSL, encoding="utf-8")
    return obj_dir


def _parse(obj_dir: Path) -> object:
    xml = obj_dir.parent / "ТестОбщаяФорма.xml"
    obj_el = first_child_element(etree.parse(str(xml)).getroot())
    return parse_object(obj_el, "base", obj_dir, fqn="CommonForm.ТестОбщаяФорма")


def test_common_form_yields_self_form(tmp_path: Path) -> None:
    obj = _parse(_make_common_form(tmp_path))

    assert obj.kind == "CommonForm"
    assert len(obj.forms) == 1, "a CommonForm must yield exactly one self-form"
    form = obj.forms[0]
    assert form.fqn == "CommonForm.ТестОбщаяФорма.Form"
    assert form.name == "ТестОбщаяФорма"
    # Resolved paths must point at the on-disk files (not the object-owned Forms/<name> layout).
    assert form.form_path is not None and Path(form.form_path).is_file()
    assert form.module_path is not None and Path(form.module_path).is_file()
    assert Path(form.form_path).name == "Form.xml"
    assert Path(form.module_path).name == "Module.bsl"


def test_common_form_text_and_handlers_are_readable(tmp_path: Path) -> None:
    obj = _parse(_make_common_form(tmp_path))
    form = obj.forms[0]

    # The resolved layout path feeds the form-text chunk.
    assert "Заголовок тестовой формы" in extract_form_text(form.form_path)

    # The resolved module path + layout feed callgraph HANDLES (event -> handler).
    handlers = {h["handler"]: h for h in parse_form_handlers(form.form_path)}
    assert handlers["ПриОткрытии"]["event"] == "OnOpen"
    assert handlers["Поле1ПриИзменении"]["event"] == "OnChange"
    assert handlers["Поле1ПриИзменении"]["element"] == "Поле1"


def test_common_form_no_layout_no_self_form(tmp_path: Path) -> None:
    """A CommonForm with neither Form.xml nor a module (e.g. ordinary/binary form) is skipped."""
    cf_dir = tmp_path / "CommonForms"
    obj_dir = cf_dir / "ТестОбщаяФорма"
    obj_dir.mkdir(parents=True)
    (cf_dir / "ТестОбщаяФорма.xml").write_text(_COMMON_FORM_XML, encoding="utf-8")

    obj = _parse(obj_dir)
    assert obj.forms == [], "no managed layout/module -> no empty Form node"


def test_common_form_chunk_text_has_no_owner_subform_duplication() -> None:
    row = {
        "owner_kind": "CommonForm",
        "owner_name": "ТестОбщаяФорма",
        "owner_syn": "Тестовая общая форма",
        "owner_fqn": "CommonForm.ТестОбщаяФорма",
        "form_fqn": "CommonForm.ТестОбщаяФорма.Form",
        "form_name": "ТестОбщаяФорма",
        "form_text": "Заголовок тестовой формы",
        "config_id": "base",
        "config_version": None,
    }
    chunk = chunking.form_chunk(row)
    # Standalone form: it must NOT read as "Общая форма «...» ▸ форма «...»".
    assert "▸ форма" not in chunk.text
    assert "Общая форма «Тестовая общая форма»" in chunk.text


def test_common_form_code_chunk_head_is_not_duplicated() -> None:
    routines = {r.name: r for r in parse_module(_FORM_MODULE_BSL)}
    rt = routines["ПриОткрытии"]
    ctx = {
        "owner_kind": "CommonForm",
        "owner_name": "ТестОбщаяФорма",
        "owner_syn": "Тестовая общая форма",
        "owner_fqn": "CommonForm.ТестОбщаяФорма",
        "module_fqn": "CommonForm.ТестОбщаяФорма.Form",
        "module_type": "FormModule",
        "form_name": "ТестОбщаяФорма",
        "config_id": "base",
        "config_version": None,
        "handlers": {"ПриОткрытии": {"event": "OnOpen", "handler": "ПриОткрытии", "element": None}},
    }
    chunks = chunking.code_chunks(rt, _FORM_MODULE_BSL, ctx)
    assert chunks, "an event handler must always produce a code chunk"
    head = chunks[0].text.splitlines()[0]
    assert "▸ форма" not in head and "FormModule" not in head
    assert chunks[0].entry_point == "событие_формы"


def test_object_owned_form_still_resolves_under_forms_dir(tmp_path: Path) -> None:
    """Regression: the generalized resolver must still find object-owned Forms/<name> layouts."""
    obj_xml = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">
  <Catalog uuid="22222222-2222-2222-2222-222222222222">
    <Properties><Name>Тест</Name></Properties>
    <ChildObjects><Form>ФормаЭлемента</Form></ChildObjects>
  </Catalog>
</MetaDataObject>
"""
    obj_dir = tmp_path / "Catalogs" / "Тест"
    form_dir = obj_dir / "Forms" / "ФормаЭлемента"
    (form_dir / "Ext" / "Form").mkdir(parents=True)
    (form_dir / "Ext" / "Form.xml").write_text(_FORM_LAYOUT_XML, encoding="utf-8")
    (form_dir / "Ext" / "Form" / "Module.bsl").write_text(_FORM_MODULE_BSL, encoding="utf-8")

    obj_el = first_child_element(etree.fromstring(obj_xml.encode("utf-8")).getroottree().getroot())
    obj = parse_object(obj_el, "base", obj_dir, fqn="Catalog.Тест")

    assert len(obj.forms) == 1
    form = obj.forms[0]
    assert form.fqn == "Catalog.Тест.Form.ФормаЭлемента"
    assert form.form_path is not None and Path(form.form_path).is_file()
    assert form.module_path is not None and Path(form.module_path).is_file()
