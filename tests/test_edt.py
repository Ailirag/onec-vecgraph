"""1C:EDT format reader — detection, project discovery, and .mdo metadata parsing.

Uses a synthetic EDT workspace (base project `conf/` + one extension project with an
Adopted object) so it runs without the real UT dump on H:.
"""

from __future__ import annotations

from pathlib import Path

from onec_vecgraph.graph.builder import build_graph
from onec_vecgraph.parsing import (
    detect_format,
    discover_parts,
    enumerate_objects,
    parse_config,
)
from onec_vecgraph.parsing.forms import extract_form_text, parse_form_handlers

_DOT_FORM = """<?xml version="1.0" encoding="UTF-8"?>
<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form" xmlns:core="http://g5.1c.ru/v8/dt/mcore">
  <title><key>ru</key><value>Форма списка контрагентов</value></title>
  <items>
    <name>Список</name>
    <handlers><event>OnActivateRow</event><name>СписокПриАктивизацииСтроки</name></handlers>
  </items>
  <handlers><event>OnCreateAtServer</event><name>ПриСозданииНаСервере</name></handlers>
</form:Form>
"""

_MDCLASS = 'xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"'

_BASE_CONFIG = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Configuration {_MDCLASS} uuid="00000000-0000-0000-0000-000000000001">
  <name>УправлениеТорговлей</name>
  <synonym><key>ru</key><value>Управление торговлей</value></synonym>
  <version>11.5.0.1</version>
</mdclass:Configuration>
"""

_EXT_CONFIG = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Configuration {_MDCLASS} uuid="00000000-0000-0000-0000-000000000002">
  <name>ДИТ_РасширениеАдаптацияУТ</name>
  <synonym><key>ru</key><value>Адаптация УТ</value></synonym>
  <objectBelonging>Adopted</objectBelonging>
  <namePrefix>ДИТ_</namePrefix>
  <configurationExtensionPurpose>Customization</configurationExtensionPurpose>
</mdclass:Configuration>
"""

_CATALOG = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Catalog {_MDCLASS} uuid="11111111-1111-1111-1111-111111111111">
  <name>Контрагенты</name>
  <synonym><key>en</key><value>Counterparties</value></synonym>
  <synonym><key>ru</key><value>Контрагенты</value></synonym>
  <hierarchical>true</hierarchical>
  <codeLength>9</codeLength>
  <attributes uuid="aaaa1111-0000-0000-0000-000000000001">
    <name>ИНН</name>
    <synonym><key>ru</key><value>ИНН</value></synonym>
    <type><types>String</types><stringQualifiers><length>12</length></stringQualifiers></type>
  </attributes>
  <attributes uuid="aaaa1111-0000-0000-0000-000000000002">
    <name>ОсновнойМенеджер</name>
    <synonym><key>ru</key><value>Основной менеджер</value></synonym>
    <type><types>CatalogRef.Пользователи</types></type>
  </attributes>
  <tabularSections uuid="bbbb1111-0000-0000-0000-000000000001">
    <name>КонтактныеЛица</name>
    <synonym><key>ru</key><value>Контактные лица</value></synonym>
    <attributes uuid="cccc1111-0000-0000-0000-000000000001">
      <name>Должность</name>
      <type><types>String</types></type>
    </attributes>
  </tabularSections>
  <forms uuid="dddd1111-0000-0000-0000-000000000001"><name>ФормаЭлемента</name></forms>
  <predefined>
    <items id="ffff1111-0000-0000-0000-000000000001">
      <name>Группа</name><description>Группа</description><isFolder>true</isFolder><code>001</code>
      <content id="ffff1111-0000-0000-0000-000000000002">
        <name>Прочее</name><description>Прочее</description><isFolder>false</isFolder><code>002</code>
      </content>
    </items>
  </predefined>
</mdclass:Catalog>
"""

_ENUM = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Enum {_MDCLASS} uuid="22222222-2222-2222-2222-222222222222">
  <name>СтатусыЗаказов</name>
  <synonym><key>ru</key><value>Статусы заказов</value></synonym>
  <enumValues uuid="eeee1111-0000-0000-0000-000000000001">
    <name>Новый</name><synonym><key>ru</key><value>Новый</value></synonym>
  </enumValues>
  <enumValues uuid="eeee1111-0000-0000-0000-000000000002">
    <name>Закрыт</name><synonym><key>ru</key><value>Закрыт</value></synonym>
  </enumValues>
</mdclass:Enum>
"""

_ADOPTED_CATALOG = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Catalog {_MDCLASS} uuid="33333333-3333-3333-3333-333333333333">
  <name>Контрагенты</name>
  <objectBelonging>Adopted</objectBelonging>
</mdclass:Catalog>
"""

_ROLE = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Role {_MDCLASS} uuid="44444444-4444-4444-4444-444444444444">
  <name>МенеджерПродаж</name>
  <synonym><key>ru</key><value>Менеджер продаж</value></synonym>
</mdclass:Role>
"""

# Rights.rights uses the same 'roles' namespace/schema as the Configurator dump.
_RIGHTS = """<?xml version="1.0" encoding="UTF-8"?>
<Rights xmlns="http://v8.1c.ru/8.2/roles" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="Rights">
  <object>
    <name>Catalog.Контрагенты</name>
    <right><name>Read</name><value>true</value></right>
    <right><name>Update</name><value>false</value></right>
  </object>
</Rights>
"""

_OBJECT_MODULE = """Процедура ПриЗаписи(Отказ)
    ПроверитьИНН();
КонецПроцедуры
"""

_OVERRIDE_MODULE = """&Вместо("ПриЗаписи")
Процедура Расш_ПриЗаписи(Отказ)
    ПроверитьЛимитКредита();
КонецПроцедуры
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_workspace(root: Path) -> Path:
    # Base project: conf/
    conf = root / "conf"
    _write(conf / "src" / "Configuration" / "Configuration.mdo", _BASE_CONFIG)
    cat = conf / "src" / "Catalogs" / "Контрагенты"
    _write(cat / "Контрагенты.mdo", _CATALOG)
    _write(cat / "ObjectModule.bsl", _OBJECT_MODULE)
    _write(cat / "Forms" / "ФормаЭлемента" / "Module.bsl", _OBJECT_MODULE)
    _write(cat / "Forms" / "ФормаЭлемента" / "Form.form", "<form/>")
    _write(conf / "src" / "Enums" / "СтатусыЗаказов" / "СтатусыЗаказов.mdo", _ENUM)
    role = conf / "src" / "Roles" / "МенеджерПродаж"
    _write(role / "МенеджерПродаж.mdo", _ROLE)
    _write(role / "Rights.rights", _RIGHTS)
    # Extension project with an Adopted (borrowed) object that overrides the base module.
    ext = root / "ДИТ_РасширениеАдаптацияУТ"
    _write(ext / "src" / "Configuration" / "Configuration.mdo", _EXT_CONFIG)
    ext_cat = ext / "src" / "Catalogs" / "Контрагенты"
    _write(ext_cat / "Контрагенты.mdo", _ADOPTED_CATALOG)
    _write(ext_cat / "ObjectModule.bsl", _OVERRIDE_MODULE)
    return root


def test_detect_edt_workspace(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path)
    assert detect_format(root) == "edt"


def test_discover_base_and_extension(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path)
    parts = discover_parts(root)
    assert [p.config_id for p in parts] == ["base", "ext:ДИТ_РасширениеАдаптацияУТ"]  # base first
    base, ext = parts
    assert base.fmt == "edt" and not base.is_extension
    assert ext.is_extension and ext.purpose == "Customization" and ext.name_prefix == "ДИТ_"


def test_enumerate_objects_have_content_hash(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path)
    refs = enumerate_objects(discover_parts(root))
    fqns = {r[0] for r in refs}
    assert {"Catalog.Контрагенты", "Enum.СтатусыЗаказов"} <= fqns
    assert all(r[4] for r in refs), "EDT objects must carry a content-hash version"


def test_parse_catalog_metadata(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path)
    parsed = parse_config(root, tenant_id="t")

    # Both base and ext produce a MetaObject for Catalog.Контрагенты (collapse happens at graph
    # build, not at parse). Select the base one here.
    cat = next(o for o in parsed.objects if o.fqn == "Catalog.Контрагенты" and o.config_id == "base")
    assert cat.synonym == "Контрагенты"  # ru preferred over en
    names = {f.name for f in cat.fields}
    assert {"ИНН", "ОсновнойМенеджер"} <= names
    mgr = next(f for f in cat.fields if f.name == "ОсновнойМенеджер")
    assert any(t.category == "reference" and t.ref_fqn == "Catalog.Пользователи" for t in mgr.types)
    assert cat.tabular and cat.tabular[0].name == "КонтактныеЛица"
    assert {m.module_type for m in cat.modules} == {"ObjectModule"}
    form = cat.forms[0]
    assert form.name == "ФормаЭлемента" and form.module_path and form.form_path


def test_edt_predefined_parsed_recursively(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path)
    parsed = parse_config(root, tenant_id="t")
    cat = next(o for o in parsed.objects if o.fqn == "Catalog.Контрагенты" and o.config_id == "base")
    pre = {p.name: p for p in cat.predefined}
    assert set(pre) == {"Группа", "Прочее"}  # folder + nested leaf
    assert pre["Группа"].is_folder and not pre["Прочее"].is_folder
    assert pre["Прочее"].fqn == "Catalog.Контрагенты.Predefined.Прочее"


def test_enum_values_parsed(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path)
    parsed = parse_config(root, tenant_id="t")
    enum = next(o for o in parsed.objects if o.fqn == "Enum.СтатусыЗаказов")
    assert {ev.name for ev in enum.enum_values} == {"Новый", "Закрыт"}


def test_adopted_object_marked(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path)
    parsed = parse_config(root, tenant_id="t")
    adopted = [o for o in parsed.objects if o.fqn == "Catalog.Контрагенты" and o.belonging == "Adopted"]
    assert adopted, "the extension's borrowed Catalog.Контрагенты must be parsed with belonging=Adopted"
    assert adopted[0].config_id == "ext:ДИТ_РасширениеАдаптацияУТ"


def test_edt_object_details_flattened(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path)
    parsed = parse_config(root, tenant_id="t")
    cat = next(o for o in parsed.objects if o.fqn == "Catalog.Контрагенты" and o.config_id == "base")
    assert cat.details.get("hierarchical") == "true"
    assert cat.details.get("codeLength") == "9"
    assert "attributes" not in cat.details and "synonym" not in cat.details  # structural excluded


def test_edt_role_rights_reused_parser(tmp_path: Path) -> None:
    root = _make_workspace(tmp_path)
    parsed = parse_config(root, tenant_id="t")
    role = next(o for o in parsed.objects if o.fqn == "Role.МенеджерПродаж")
    rights = {rr.object_fqn: rr.rights for rr in role.rights}
    assert rights["Catalog.Контрагенты"] == {"Read": True, "Update": False}


def test_edt_form_handlers_and_text(tmp_path: Path) -> None:
    """R2.4: the .form reader (dispatched by extension in parsing.forms) extracts EDT form
    event→handler wiring and human-readable captions."""
    form = tmp_path / "Form.form"
    form.write_text(_DOT_FORM, encoding="utf-8")

    handlers = {h["event"]: h for h in parse_form_handlers(str(form))}
    assert handlers["OnActivateRow"]["handler"] == "СписокПриАктивизацииСтроки"
    assert handlers["OnActivateRow"]["element"] == "Список"
    assert handlers["OnCreateAtServer"]["handler"] == "ПриСозданииНаСервере"

    assert "Форма списка контрагентов" in extract_form_text(str(form))


def test_adopted_overrides_qualified_alongside_base(tmp_path: Path) -> None:
    """R1.1: a borrowed object's module attaches to the SAME Object node under a config-qualified
    fqn, so the extension override is kept instead of collapsing onto (and losing to) the base."""
    root = _make_workspace(tmp_path)
    parsed = parse_config(root, tenant_id="t")
    graph = build_graph(parsed)

    module_fqns = {n["fqn"] for n in graph.nodes.get("Module", [])}
    base_mod = "Catalog.Контрагенты.Module.ObjectModule"
    ext_mod = "Catalog.Контрагенты.Module.ObjectModule@ext:ДИТ_РасширениеАдаптацияУТ"
    assert base_mod in module_fqns, "base object module present"
    assert ext_mod in module_fqns, "extension override module kept as a qualified sibling"

    # Both HAS_MODULE edges point at the SINGLE (base) Object node — one logical object.
    has_module = next(g for g in graph.edge_groups() if g.rel == "HAS_MODULE")
    targets = {r["dst"] for r in has_module.rows if r["src"] == "Catalog.Контрагенты"}
    assert {base_mod, ext_mod} <= targets

    # Exactly one Object node for the fqn (base wins the card; no duplicate object).
    objects = [n for n in graph.nodes.get("Object", []) if n["fqn"] == "Catalog.Контрагенты"]
    assert len(objects) == 1 and objects[0]["config_id"] == "base"
