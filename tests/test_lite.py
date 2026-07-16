"""Lite MCP server: zero-infrastructure navigation over a live working copy.

Uses synthetic workspaces (EDT: base + extension; Configurator: minimal dump) so tests
run without Neo4j, embeddings, real dumps or ripgrep (the Python search fallback is
forced; rg-specific behavior is covered implicitly when rg is present on the machine).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from onec_vecgraph.lite import Workspace, code_intel
from onec_vecgraph.lite import search as lite_search
from onec_vecgraph.lite import server as lite_server

_MDCLASS = 'xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"'

_BASE_CONFIG = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Configuration {_MDCLASS} uuid="00000000-0000-0000-0000-000000000001">
  <name>УправлениеТорговлей</name>
  <synonym><key>ru</key><value>Управление торговлей</value></synonym>
</mdclass:Configuration>
"""

_EXT_CONFIG = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Configuration {_MDCLASS} uuid="00000000-0000-0000-0000-000000000002">
  <name>ДИТ_Расширение</name>
  <objectBelonging>Adopted</objectBelonging>
  <configurationExtensionPurpose>Customization</configurationExtensionPurpose>
</mdclass:Configuration>
"""

_CATALOG = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Catalog {_MDCLASS} uuid="11111111-1111-1111-1111-111111111111">
  <name>Контрагенты</name>
  <synonym><key>ru</key><value>Клиенты и поставщики</value></synonym>
  <attributes uuid="aaaa1111-0000-0000-0000-000000000001">
    <name>ИНН</name>
    <type><types>String</types></type>
  </attributes>
  <forms uuid="dddd1111-0000-0000-0000-000000000001"><name>ФормаЭлемента</name></forms>
</mdclass:Catalog>
"""

_ADOPTED_CATALOG = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Catalog {_MDCLASS} uuid="11111111-1111-1111-1111-111111111111">
  <name>Контрагенты</name>
  <objectBelonging>Adopted</objectBelonging>
</mdclass:Catalog>
"""

_COMMON_MODULE = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:CommonModule {_MDCLASS} uuid="22222222-2222-2222-2222-222222222222">
  <name>РаботаСИНН</name>
  <server>true</server>
</mdclass:CommonModule>
"""

_DOCUMENT = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Document {_MDCLASS} uuid="33333333-3333-3333-3333-333333333333">
  <name>ЗаказКлиента</name>
  <synonym><key>ru</key><value>Заказ клиента</value></synonym>
  <attributes uuid="33333333-0000-0000-0000-000000000001">
    <name>Контрагент</name>
    <type><types>CatalogRef.Контрагенты</types></type>
  </attributes>
  <registerRecords>AccumulationRegister.ОстаткиТоваров</registerRecords>
</mdclass:Document>
"""

_SUBSCRIPTION = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:EventSubscription {_MDCLASS} uuid="77777777-7777-7777-7777-777777777777">
  <name>ПриЗаписиКонтрагента</name>
  <source><types>CatalogObject.Контрагенты</types></source>
  <event>OnWrite</event>
  <handler>CommonModule.РаботаСИНН.ПроверитьИНН</handler>
</mdclass:EventSubscription>
"""

_DOT_FORM = """<?xml version="1.0" encoding="UTF-8"?>
<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form"
           xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
           xmlns:core="http://g5.1c.ru/v8/dt/mcore">
  <items xsi:type="form:FormField">
    <name>Список</name>
    <dataPath xsi:type="form:DataPath"><segments>Объект.Список</segments></dataPath>
    <handlers><event>OnActivateRow</event><name>СписокПриАктивизацииСтроки</name></handlers>
    <type>InputField</type>
  </items>
  <items xsi:type="form:FormGroup">
    <name>ГруппаКоманд</name>
    <items xsi:type="form:Button">
      <name>КнопкаПоиск</name>
      <commandName>Form.Command.ПоискПоШтрихкоду</commandName>
    </items>
  </items>
  <formCommands>
    <name>ПоискПоШтрихкоду</name>
    <title><key>ru</key><value>Поиск по штрихкоду</value></title>
    <shortcut>F7</shortcut>
    <action xsi:type="form:FormCommandHandlerContainer">
      <handler><name>ПоискВыполнить</name></handler>
    </action>
  </formCommands>
  <attributes>
    <name>Объект</name>
    <valueType><types>CatalogObject.Контрагенты</types></valueType>
  </attributes>
  <handlers><event>OnCreateAtServer</event><name>ПриСозданииНаСервере</name></handlers>
</form:Form>
"""

_HTTP_SERVICE = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:HTTPService {_MDCLASS} uuid="55555555-5555-5555-5555-555555555555">
  <name>Api</name>
  <rootURL>api</rootURL>
  <urlTemplates uuid="55555555-0000-0000-0000-000000000001">
    <name>Версия</name>
    <template>/version</template>
    <methods uuid="55555555-0000-0000-0000-000000000002">
      <name>Получить</name>
      <httpMethod>GET</httpMethod>
      <handler>ВерсияПолучить</handler>
    </methods>
    <methods uuid="55555555-0000-0000-0000-000000000003">
      <name>Пинг</name>
      <handler>Пинг</handler>
    </methods>
  </urlTemplates>
</mdclass:HTTPService>
"""

_HTTP_SERVICE_BSL = """Функция ВерсияПолучить(Запрос) Экспорт
    Возврат Неопределено;
КонецФункции
"""

_WEB_SERVICE = f"""<?xml version="1.0" encoding="UTF-8"?>
<mdclass:WebService {_MDCLASS} uuid="66666666-6666-6666-6666-666666666666">
  <name>Обмен</name>
  <namespace>http://x</namespace>
  <operations uuid="66666666-0000-0000-0000-000000000001">
    <name>execute</name>
    <procedureName>ОбработатьЗапрос</procedureName>
    <xdtoReturningValueType><name>Resp</name></xdtoReturningValueType>
    <parameters uuid="66666666-0000-0000-0000-000000000002">
      <name>request</name>
      <xdtoValueType><name>Req</name></xdtoValueType>
    </parameters>
  </operations>
</mdclass:WebService>
"""

_COMMON_BSL = """Функция ПроверитьИНН(Знач ИНН) Экспорт
    Возврат СтрДлина(ИНН) = 10;
КонецФункции

Процедура ВспомогательнаяВещь()
    Если Не ПроверитьИНН("0000000000") Тогда
        ВызватьИсключение "плохой ИНН";
    КонецЕсли;
КонецПроцедуры
"""

_CATALOG_OBJECT_BSL = """Процедура ПередЗаписью(Отказ)
    Если Не ПроверитьЗаполнение() Тогда
        Отказ = Истина;
    КонецЕсли;
КонецПроцедуры

Функция ПроверитьЗаполнение()
    Возврат РаботаСИНН.ПроверитьИНН(ИНН);
КонецФункции
"""

_CATALOG_MANAGER_BSL = """Функция СоздатьПоИНН(Знач ИНН) Экспорт
    Новый = СоздатьЭлемент();
    Возврат Новый;
КонецФункции
"""

_DOCUMENT_OBJECT_BSL = """Процедура ОбработкаПроведения(Отказ, РежимПроведения)
    Если Не РаботаСИНН.ПроверитьИНН(Контрагент.ИНН) Тогда
        Отказ = Истина;
    КонецЕсли;
    Справочники.Контрагенты.СоздатьПоИНН("1234567890");
КонецПроцедуры

Процедура Пустышка()
    // Комментарий: ПроверитьИНН("не вызов")
    Стр = "ПроверитьИНН(тоже не вызов)";
КонецПроцедуры
"""

_FORM_BSL = """&НаСервере
Процедура ПриСозданииНаСервере(Отказ, СтандартнаяОбработка)
    РаботаСИНН.ПроверитьИНН("1");
КонецПроцедуры

&НаКлиенте
Процедура СписокПриАктивизацииСтроки(Элемент)
КонецПроцедуры

&НаКлиенте
Процедура ПоискВыполнить(Команда)
КонецПроцедуры
"""

_EXT_OBJECT_BSL = """&Вместо("ПередЗаписью")
Процедура ДИТ_ПередЗаписью(Отказ)
    ПродолжитьВызов(Отказ);
КонецПроцедуры
"""

# Форма расширения: EDT хранит только дерево элементов (без attributes/formCommands).
_EXT_FORM = """<?xml version="1.0" encoding="UTF-8"?>
<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form"
           xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <items xsi:type="form:FormField">
    <name>ИНН</name>
    <dataPath xsi:type="form:DataPath"><segments>Объект.ИНН</segments></dataPath>
    <handlers><event>OnChange</event><name>ДИТ_ИННПриИзменении</name></handlers>
  </items>
</form:Form>
"""

_EXT_FORM_BSL = """&НаКлиенте
Процедура ДИТ_ИННПриИзменении(Элемент)
КонецПроцедуры
"""


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(scope="module")
def edt_ws(tmp_path_factory: pytest.TempPathFactory) -> Workspace:
    root = tmp_path_factory.mktemp("edt_ws")
    conf = root / "conf" / "src"
    _w(conf / "Configuration" / "Configuration.mdo", _BASE_CONFIG)
    cat = conf / "Catalogs" / "Контрагенты"
    _w(cat / "Контрагенты.mdo", _CATALOG)
    _w(cat / "ObjectModule.bsl", _CATALOG_OBJECT_BSL)
    _w(cat / "ManagerModule.bsl", _CATALOG_MANAGER_BSL)
    _w(cat / "Forms" / "ФормаЭлемента" / "Form.form", _DOT_FORM)
    _w(cat / "Forms" / "ФормаЭлемента" / "Module.bsl", _FORM_BSL)
    _w(conf / "CommonModules" / "РаботаСИНН" / "РаботаСИНН.mdo", _COMMON_MODULE)
    _w(conf / "CommonModules" / "РаботаСИНН" / "Module.bsl", _COMMON_BSL)
    doc = conf / "Documents" / "ЗаказКлиента"
    _w(doc / "ЗаказКлиента.mdo", _DOCUMENT)
    _w(doc / "ObjectModule.bsl", _DOCUMENT_OBJECT_BSL)
    svc = conf / "HTTPServices" / "Api"
    _w(svc / "Api.mdo", _HTTP_SERVICE)
    _w(svc / "Module.bsl", _HTTP_SERVICE_BSL)
    _w(conf / "WebServices" / "Обмен" / "Обмен.mdo", _WEB_SERVICE)
    _w(conf / "EventSubscriptions" / "ПриЗаписиКонтрагента" / "ПриЗаписиКонтрагента.mdo",
       _SUBSCRIPTION)

    ext = root / "dit_ext" / "src"
    _w(ext / "Configuration" / "Configuration.mdo", _EXT_CONFIG)
    _w(ext / "Catalogs" / "Контрагенты" / "Контрагенты.mdo", _ADOPTED_CATALOG)
    _w(ext / "Catalogs" / "Контрагенты" / "ObjectModule.bsl", _EXT_OBJECT_BSL)
    ext_form = ext / "Catalogs" / "Контрагенты" / "Forms" / "ФормаЭлемента"
    _w(ext_form / "Form.form", _EXT_FORM)
    _w(ext_form / "Module.bsl", _EXT_FORM_BSL)
    return Workspace(root)


@pytest.fixture(autouse=True)
def _no_rg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the pure-Python search path so tests do not depend on ripgrep."""
    monkeypatch.setattr(lite_search, "rg_path", lambda: None)


# --------------------------------------------------------------------------- #
# Workspace
# --------------------------------------------------------------------------- #

def test_discovery_extension_first(edt_ws: Workspace) -> None:
    names = [s.name for s in edt_ws.sources]
    assert names == ["ДИТ_Расширение", "УправлениеТорговлей"]
    assert edt_ws.sources[0].is_extension and not edt_ws.sources[1].is_extension
    assert {s.fmt for s in edt_ws.sources} == {"edt"}


def test_kind_counts(edt_ws: Workspace) -> None:
    base = edt_ws.sources[1]
    assert edt_ws.kind_counts(base) == {
        "Catalog": 1, "CommonModule": 1, "Document": 1, "EventSubscription": 1,
        "HTTPService": 1, "WebService": 1,
    }


def test_find_object_prefers_extension(edt_ws: Workspace) -> None:
    src, ref, also, err = edt_ws.find_object("Catalog", "Контрагенты")
    assert err is None and src is not None and ref is not None
    assert src.name == "ДИТ_Расширение"
    assert also == ["УправлениеТорговлей"]
    src2, *_rest, err2 = edt_ws.find_object("Catalog", "Контрагенты", "УправлениеТорговлей")
    assert err2 is None and src2.name == "УправлениеТорговлей"


def test_module_alias_and_form_resolution(edt_ws: Workspace) -> None:
    base = edt_ws.sources[1]
    path, msg = edt_ws.module_path(base, "Catalog", "Контрагенты", "Object")
    assert msg == "" and path is not None and path.name == "ObjectModule.bsl"
    fpath, msg2 = edt_ws.module_path(base, "Catalog", "Контрагенты", "Form:ФормаЭлемента")
    assert msg2 == "" and fpath is not None and fpath.parent.name == "ФормаЭлемента"
    missing, msg3 = edt_ws.module_path(base, "Catalog", "Контрагенты", "RecordSet")
    assert missing is None and "Доступны" in msg3 and "Form:ФормаЭлемента" in msg3


def test_read_file_refuses_escape(edt_ws: Workspace) -> None:
    base = edt_ws.sources[1]
    path, msg = edt_ws.safe_path(base, "../../dit_ext/src/Configuration/Configuration.mdo")
    assert path is None and "за пределы" in msg


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #

def test_search_code_python_fallback(edt_ws: Workspace) -> None:
    res = lite_search.search_code(edt_ws, r"ПроверитьИНН\s*\(", max_results=50)
    assert res["engine"] == "python"
    paths = {m["path"] for m in res["matches"]}
    assert any("CommonModules/РаботаСИНН/Module.bsl" in p for p in paths)
    assert any("Documents/ЗаказКлиента/ObjectModule.bsl" in p for p in paths)
    assert all(m["source"] in ("УправлениеТорговлей", "ДИТ_Расширение") for m in res["matches"])


def test_search_code_name_filter(edt_ws: Workspace) -> None:
    res = lite_search.search_code(edt_ws, "ПроверитьИНН", regex=False, name_filter="заказ")
    assert res["matches"] and all("ЗаказКлиента" in m["path"] for m in res["matches"])


# --------------------------------------------------------------------------- #
# Code intel
# --------------------------------------------------------------------------- #

def test_find_declarations(edt_ws: Workspace) -> None:
    res = code_intel.find_declarations(edt_ws, "ПроверитьИНН", exported_only=True)
    decls = res["declarations"]
    assert len(decls) == 1
    d = decls[0]
    assert d["object"] == "CommonModule.РаботаСИНН" and d["export"] is True
    assert d["kind"] == "Function" and d["signature"].startswith("Функция ПроверитьИНН")


def test_find_callees_resolves_local_common_manager(edt_ws: Workspace) -> None:
    res = code_intel.find_callees(
        edt_ws, "Document", "ЗаказКлиента", "Object", "ОбработкаПроведения")
    kinds = {c["kind"]: c for c in res["resolved"]}
    assert "common_module" in kinds and "manager" in kinds
    assert kinds["common_module"]["target"].endswith("::ПроверитьИНН")
    assert kinds["common_module"]["confidence"] == "high"
    assert kinds["manager"]["target"] == "Catalog.Контрагенты::СоздатьПоИНН"
    assert kinds["manager"]["confidence"] == "medium"

    res2 = code_intel.find_callees(
        edt_ws, "Catalog", "Контрагенты", "Object", "ПередЗаписью",
        source="УправлениеТорговлей")
    local = [c for c in res2["resolved"] if c["kind"] == "local"]
    assert local and local[0]["routine"] == "ПроверитьЗаполнение"

    # Заимствованный объект без source: модуль расширения не содержит базовой рутины —
    # фолбэк должен провалиться до базы, а не ошибиться.
    res3 = code_intel.find_callees(edt_ws, "Catalog", "Контрагенты", "Object", "ПередЗаписью")
    assert res3.get("source") == "УправлениеТорговлей"
    assert res3["also_in"] == ["ДИТ_Расширение"]


def test_find_callers_verified(edt_ws: Workspace) -> None:
    res = code_intel.find_callers(edt_ws, "ПроверитьИНН")
    rows = res["callers"]
    by_routine = {r["routine"] for r in rows}
    assert {"ОбработкаПроведения", "ПриСозданииНаСервере", "ВспомогательнаяВещь"} <= by_routine
    assert "Пустышка" not in by_routine  # строка и комментарий — не вызовы
    assert "ПроверитьИНН" not in by_routine  # объявление исключено
    local_rows = [r for r in rows if r["routine"] == "ВспомогательнаяВещь"]
    assert local_rows[0]["qualifier"] is None and local_rows[0]["local_target"] is True
    qualified = [r for r in rows if r["routine"] == "ОбработкаПроведения"]
    assert qualified[0]["qualifier"] == "РаботаСИНН"


def test_find_callers_object_hint(edt_ws: Workspace) -> None:
    res = code_intel.find_callers(edt_ws, "ПроверитьИНН", object_hint="РаботаСИНН")
    routines = {r["routine"] for r in res["callers"]}
    assert "ОбработкаПроведения" in routines and "ПриСозданииНаСервере" in routines
    # локальный вызов внутри самого модуля РаботаСИНН тоже проходит по hint
    assert "ВспомогательнаяВещь" in routines


def test_call_graph_upward(edt_ws: Workspace) -> None:
    res = code_intel.call_graph(edt_ws, "СоздатьПоИНН", depth=2)
    assert res["depth"] >= 1
    level1 = {r["routine"] for r in res["levels"][0]}
    assert "ОбработкаПроведения" in level1
    assert all(r["calls"] == "СоздатьПоИНН" for r in res["levels"][0])


def test_find_overrides(edt_ws: Workspace) -> None:
    res = code_intel.find_overrides(edt_ws)
    assert res["override_count"] == 1
    o = res["overrides"][0]
    assert o["source"] == "ДИТ_Расширение"
    assert o["object"] == "Catalog.Контрагенты"
    assert o["mode"] == "Вместо" and o["target"] == "ПередЗаписью"
    assert o["routine"] == "ДИТ_ПередЗаписью"
    assert code_intel.find_overrides(edt_ws, method="ПередЗаписью")["override_count"] == 1
    assert code_intel.find_overrides(edt_ws, method="Другое")["override_count"] == 0


def test_find_handlers_merged_across_sources(edt_ws: Workspace) -> None:
    # Без source: заимствованный объект собирается из расширения И базы (платформенный вид).
    res = code_intel.find_handlers(edt_ws, "Catalog", "Контрагенты")
    assert res["sources"] == ["ДИТ_Расширение", "УправлениеТорговлей"]
    events = {(h["event"], h["handler"], h["declared"]) for h in res["form_handlers"]}
    assert ("OnCreateAtServer", "ПриСозданииНаСервере", True) in events
    assert ("OnActivateRow", "СписокПриАктивизацииСтроки", True) in events
    eps = {(m["routine"], m["entry_point"], m["source"]) for m in res["module_entry_points"]}
    assert ("ПередЗаписью", "запись", "УправлениеТорговлей") in eps

    res2 = code_intel.find_handlers(edt_ws, "Document", "ЗаказКлиента")
    eps2 = {(m["routine"], m["entry_point"]) for m in res2["module_entry_points"]}
    assert ("ОбработкаПроведения", "проведение") in eps2


def test_writes_to(edt_ws: Workspace) -> None:
    fwd = code_intel.writes_to(edt_ws, document="ЗаказКлиента")
    assert fwd["registers"] == ["AccumulationRegister.ОстаткиТоваров"]
    rev = code_intel.writes_to(edt_ws, register="ОстаткиТоваров")
    assert rev["writer_count"] == 1
    assert rev["writers"][0]["document"] == "Document.ЗаказКлиента"


def test_metrics(edt_ws: Workspace) -> None:
    res = code_intel.metrics(edt_ws)
    rows = {r["source"]: r for r in res["sources"]}
    base = rows["УправлениеТорговлей"]
    assert base["objects_by_kind"]["Catalog"] == 1
    # модули справочника (объектный/менеджерский/форменный), общий модуль, модуль
    # документа, модуль HTTP-сервиса
    assert base["bsl_files"] == 6 and base["code_bytes"] > 0
    assert base["routines"] is None  # rg отключён фикстурой — честный None, не ноль


# --------------------------------------------------------------------------- #
# MCP server tools (over the same workspace)
# --------------------------------------------------------------------------- #

@pytest.fixture()
def served(edt_ws: Workspace, monkeypatch: pytest.MonkeyPatch,
           tmp_path: Path) -> Workspace:
    monkeypatch.setenv("ONEC_LITE_STATE", str(tmp_path / "state.json"))
    monkeypatch.delenv("ONEC_LITE_WORKSPACE", raising=False)
    monkeypatch.setattr(lite_server, "_WORKSPACES", {"default": edt_ws})
    return edt_ws


def test_tool_overview(served: Workspace) -> None:
    res = lite_server.overview()
    assert res["resolution_order"] == ["ДИТ_Расширение", "УправлениеТорговлей"]
    ext = res["sources"][0]
    assert ext["is_extension"] and ext["objects_by_kind"]["Catalog"] == 1


def test_tool_list_objects_multi_source(served: Workspace) -> None:
    res = lite_server.list_objects("Catalog")
    assert res["count"] == 1
    row = res["objects"][0]
    assert row["name"] == "Контрагенты" and row["source"] == "ДИТ_Расширение"
    assert row["in_multiple_sources"] == ["ДИТ_Расширение", "УправлениеТорговлей"]


def test_tool_get_object(served: Workspace) -> None:
    res = lite_server.get_object("Catalog", "Контрагенты", source="УправлениеТорговлей")
    assert res["synonym"] == "Клиенты и поставщики"
    assert [a["name"] for a in res["attributes"]] == ["ИНН"]
    assert res["forms"] == [{"name": "ФормаЭлемента", "has_module": True, "has_layout": True}]
    assert {m["module"] for m in res["modules"]} == {"ObjectModule", "ManagerModule"}
    doc = lite_server.get_object("Document", "ЗаказКлиента")
    assert doc["register_records"] == ["AccumulationRegister.ОстаткиТоваров"]


def test_tool_read_routine_and_module(served: Workspace) -> None:
    res = lite_server.read_routine("CommonModule", "РаботаСИНН", "ПроверитьИНН")
    assert res["export"] is True and "СтрДлина" in res["text"]
    mod = lite_server.read_module("CommonModule", "РаботаСИНН", start_line=1, max_lines=2)
    assert mod["total_lines"] > 2 and mod["end_line"] == 2


def test_tool_read_routine_adopted_fallback(served: Workspace) -> None:
    # Объект резолвится в расширение (extension-first), но рутина есть только в базовом
    # модуле — read_routine обязан найти её через фолбэк по источникам.
    res = lite_server.read_routine("Catalog", "Контрагенты", "ПередЗаписью", module="Object")
    assert res.get("error") is None
    assert res["source"] == "УправлениеТорговлей"
    assert "ПроверитьЗаполнение" in res["text"]
    # А хук расширения на том же объекте находится в расширении.
    res2 = lite_server.read_routine("Catalog", "Контрагенты", "ДИТ_ПередЗаписью",
                                    module="Object")
    assert res2.get("error") is None and res2["source"] == "ДИТ_Расширение"


def test_tool_search_metadata(served: Workspace) -> None:
    res = lite_server.search_metadata("поставщики")
    matched = {(m["object"], m["matched"]) for m in res["matches"]}
    assert ("Catalog.Контрагенты", "text") in matched
    by_name = lite_server.search_metadata("Заказ")
    assert ("Document.ЗаказКлиента", "name") in {(m["object"], m["matched"])
                                                 for m in by_name["matches"]}


def test_tool_bad_kind_and_unknown_source(served: Workspace) -> None:
    assert "error" in lite_server.list_objects("Catalogue")
    assert "error" in lite_server.get_object("Catalog", "Контрагенты", source="нет_такого")


# --------------------------------------------------------------------------- #
# Configurator-format workspace
# --------------------------------------------------------------------------- #

_CFG_MD = 'xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core"'

_CFG_CONFIGURATION = f"""<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject {_CFG_MD}>
  <Configuration uuid="00000000-0000-0000-0000-00000000000a">
    <Properties>
      <Name>Тестовая</Name>
    </Properties>
  </Configuration>
</MetaDataObject>
"""

_CFG_CATALOG = f"""<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject {_CFG_MD}>
  <Catalog uuid="11111111-0000-0000-0000-00000000000a">
    <Properties>
      <Name>Товары</Name>
      <Synonym>
        <v8:item><v8:lang>ru</v8:lang><v8:content>Номенклатура товаров</v8:content></v8:item>
      </Synonym>
    </Properties>
    <ChildObjects>
      <Attribute uuid="22222222-0000-0000-0000-00000000000a">
        <Properties>
          <Name>Артикул</Name>
          <Type><v8:Type>xs:string</v8:Type></Type>
        </Properties>
      </Attribute>
    </ChildObjects>
  </Catalog>
</MetaDataObject>
"""

_CFG_OBJECT_BSL = """Процедура ПриЗаписи(Отказ)
    ПроверитьАртикул();
КонецПроцедуры

Функция ПроверитьАртикул()
    Возврат Истина;
КонецФункции
"""

_CFG_FORM = """<?xml version="1.0" encoding="UTF-8"?>
<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" xmlns:v8="http://v8.1c.ru/8.1/data/core"
      xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config">
  <Attributes>
    <Attribute name="Объект" id="1">
      <Type><v8:Type>cfg:CatalogObject.Товары</v8:Type></Type>
      <MainAttribute>true</MainAttribute>
    </Attribute>
  </Attributes>
  <Commands>
    <Command name="Пересчитать" id="1">
      <Title><v8:item><v8:lang>ru</v8:lang><v8:content>Пересчитать всё</v8:content></v8:item></Title>
      <Action>Пересчитать</Action>
    </Command>
  </Commands>
  <Events>
    <Event name="OnOpen">ПриОткрытии</Event>
  </Events>
  <ChildItems>
    <InputField name="Артикул" id="2">
      <DataPath>Объект.Артикул</DataPath>
      <Events><Event name="OnChange">АртикулПриИзменении</Event></Events>
    </InputField>
    <Button name="КнопкаПересчитать" id="3">
      <CommandName>Form.Command.Пересчитать</CommandName>
    </Button>
    <UsualGroup name="Группа" id="4">
      <ChildItems>
        <CheckBoxField name="Флаг" id="5"><DataPath>Объект.Флаг</DataPath></CheckBoxField>
      </ChildItems>
    </UsualGroup>
  </ChildItems>
</Form>
"""

_CFG_FORM_BSL = """&НаКлиенте
Процедура ПриОткрытии(Отказ)
КонецПроцедуры

&НаКлиенте
Процедура АртикулПриИзменении(Элемент)
КонецПроцедуры

&НаКлиенте
Процедура Пересчитать(Команда)
КонецПроцедуры
"""


@pytest.fixture(scope="module")
def cfg_ws(tmp_path_factory: pytest.TempPathFactory) -> Workspace:
    root = tmp_path_factory.mktemp("cfg_dump")
    _w(root / "Configuration.xml", _CFG_CONFIGURATION)
    _w(root / "Catalogs" / "Товары.xml", _CFG_CATALOG)
    _w(root / "Catalogs" / "Товары" / "Ext" / "ObjectModule.bsl", _CFG_OBJECT_BSL)
    form_dir = root / "Catalogs" / "Товары" / "Forms" / "Форма" / "Ext"
    _w(form_dir / "Form.xml", _CFG_FORM)
    _w(form_dir / "Form" / "Module.bsl", _CFG_FORM_BSL)
    return Workspace(root)


def test_configurator_workspace(cfg_ws: Workspace) -> None:
    assert [s.fmt for s in cfg_ws.sources] == ["configurator"]
    src = cfg_ws.sources[0]
    assert cfg_ws.kind_counts(src) == {"Catalog": 1}

    _src, ref, _also, err = cfg_ws.find_object("Catalog", "Товары")
    assert err is None and ref is not None
    obj = cfg_ws.parse_object(src, ref)
    assert obj.synonym == "Номенклатура товаров"
    assert [f.name for f in obj.fields] == ["Артикул"]

    path, msg = cfg_ws.module_path(src, "Catalog", "Товары", "Object")
    assert msg == "" and path is not None and path.parent.name == "Ext"
    assert code_intel.describe_bsl_path(
        src, "Catalogs/Товары/Ext/ObjectModule.bsl"
    ) == {"kind": "Catalog", "object": "Catalog.Товары", "module": "ObjectModule"}


def test_configurator_callers_and_search(cfg_ws: Workspace) -> None:
    res = code_intel.find_callers(cfg_ws, "ПроверитьАртикул")
    assert [r["routine"] for r in res["callers"]] == ["ПриЗаписи"]
    found = lite_search.search_code(cfg_ws, "ПроверитьАртикул", regex=False)
    assert found["engine"] == "python" and found["match_count"] >= 2


# --------------------------------------------------------------------------- #
# Зависимости и подписки (get_dependencies / find_type_usages)
# --------------------------------------------------------------------------- #

def test_get_dependencies_outgoing_and_incoming(served: Workspace) -> None:
    doc = lite_server.get_dependencies("Document", "ЗаказКлиента")
    assert doc["references"] == [{"target": "Catalog.Контрагенты", "attributes": ["Контрагент"]}]
    assert doc["register_records"] == ["AccumulationRegister.ОстаткиТоваров"]

    cat = lite_server.get_dependencies("Catalog", "Контрагенты")
    assert cat["source"] == "ДИТ_Расширение" and cat["also_in"] == ["УправлениеТорговлей"]
    ref_objs = {r["object"] for r in cat["referenced_by"]}
    assert "Document.ЗаказКлиента" in ref_objs  # реквизит документа
    assert "EventSubscription.ПриЗаписиКонтрагента" in ref_objs  # источник подписки
    subs = cat["subscriptions"]
    assert subs == [{
        "source": "УправлениеТорговлей", "subscription": "ПриЗаписиКонтрагента",
        "event": "OnWrite", "handler": "CommonModule.РаботаСИНН.ПроверитьИНН",
    }]


def test_find_type_usages_rows(served: Workspace) -> None:
    res = lite_server.find_type_usages("Catalog", "Контрагенты")
    by_artifact: dict[str, set[str]] = {}
    for u in res["usages"]:
        by_artifact.setdefault(u["artifact"], set()).add(u["object"])
    assert "Document.ЗаказКлиента" in by_artifact["meta"]
    # реквизит формы (CatalogObject.Контрагенты в _DOT_FORM) виден как form_layout
    assert "Catalog.Контрагенты" in by_artifact["form_layout"]
    assert all(u["line"] > 0 and u["text"] for u in res["usages"])
    assert res["truncated"] is False


def test_find_handlers_includes_subscriptions(served: Workspace) -> None:
    res = code_intel.find_handlers(served, "Catalog", "Контрагенты",
                                   source="УправлениеТорговлей")
    assert [s["subscription"] for s in res["subscriptions"]] == ["ПриЗаписиКонтрагента"]


# --------------------------------------------------------------------------- #
# Сервисы и формы (get_service / get_form)
# --------------------------------------------------------------------------- #

def test_get_service_http(served: Workspace) -> None:
    res = lite_server.get_service("Api")
    assert res["kind"] == "HTTPService" and res["root_url"] == "api"
    assert res["source"] == "УправлениеТорговлей"
    tpl = res["url_templates"][0]
    assert tpl["name"] == "Версия" and tpl["template"] == "/version"
    methods = {m["name"]: m for m in tpl["methods"]}
    assert methods["Получить"]["http_method"] == "GET"
    assert methods["Получить"]["handler"] == "ВерсияПолучить"
    assert methods["Получить"]["declared"] is True and methods["Получить"]["lines"]
    assert methods["Пинг"]["http_method"] == "ANY"
    assert methods["Пинг"]["declared"] is False  # обработчик не реализован в модуле
    assert res["module_path"].endswith("Module.bsl")


def test_get_service_web_and_missing(served: Workspace) -> None:
    res = lite_server.get_service("Обмен")
    assert res["kind"] == "WebService" and res["namespace"] == "http://x"
    op = res["operations"][0]
    assert op["name"] == "execute" and op["handler"] == "ОбработатьЗапрос"
    assert op["returns"] == "Resp"
    assert op["parameters"] == [{"name": "request", "type": "Req"}]
    assert op["declared"] is False and res["module_path"] is None  # модуля у сервиса нет
    assert "не найден" in lite_server.get_service("НетТакого")["error"]


def test_get_form_edt(served: Workspace) -> None:
    res = lite_server.get_form("Catalog", "Контрагенты", "ФормаЭлемента",
                               source="УправлениеТорговлей")
    assert res["counts"] == {"attributes": 1, "commands": 1, "items": 3}
    attr = res["attributes"][0]
    assert attr["name"] == "Объект" and attr["type"] == "CatalogObject.Контрагенты"
    cmd = res["commands"][0]
    assert cmd["name"] == "ПоискПоШтрихкоду" and cmd["handler"] == "ПоискВыполнить"
    assert cmd["declared"] is True and cmd["shortcut"] == "F7"
    assert cmd["title"] == "Поиск по штрихкоду"
    items = {i["name"]: i for i in res["items"]}
    assert items["Список"]["data_path"] == "Объект.Список"
    assert items["Список"]["handlers"][0]["declared"] is True
    assert items["КнопкаПоиск"]["command"] == "ПоискПоШтрихкоду"
    assert items["КнопкаПоиск"]["path"] == "ГруппаКоманд"
    fh = res["form_handlers"][0]
    assert fh["event"] == "OnCreateAtServer" and fh["declared"] is True


def test_get_form_extension_merges_base_sections(served: Workspace) -> None:
    res = lite_server.get_form("Catalog", "Контрагенты", "ФормаЭлемента")  # ext-first
    assert res["source"] == "ДИТ_Расширение"
    items = {i["name"]: i for i in res["items"]}
    assert items["ИНН"]["handlers"][0]["declared"] is True  # по модулю формы расширения
    # реквизиты/команды у формы расширения отсутствуют -> достроены из базовой
    assert res["attributes"][0]["name"] == "Объект"
    assert res["attributes_source"] == "УправлениеТорговлей"
    assert res["commands"][0]["name"] == "ПоискПоШтрихкоду"
    assert res["commands_source"] == "УправлениеТорговлей"
    assert res["commands"][0]["declared"] is True  # по модулю БАЗОВОЙ формы


def test_get_form_missing_and_no_name(served: Workspace) -> None:
    err = lite_server.get_form("Catalog", "Контрагенты", "НетТакой")["error"]
    assert "ФормаЭлемента" in err  # подсказка со списком доступных форм
    assert "Укажите имя формы" in lite_server.get_form("Catalog", "Контрагенты")["error"]


def test_get_form_configurator(cfg_ws: Workspace, monkeypatch: pytest.MonkeyPatch,
                               tmp_path: Path) -> None:
    monkeypatch.setenv("ONEC_LITE_STATE", str(tmp_path / "state.json"))
    monkeypatch.delenv("ONEC_LITE_WORKSPACE", raising=False)
    monkeypatch.setattr(lite_server, "_WORKSPACES", {"default": cfg_ws})
    res = lite_server.get_form("Catalog", "Товары", "Форма")
    attr = res["attributes"][0]
    assert attr["name"] == "Объект" and attr.get("main") is True
    assert "CatalogObject.Товары" in attr["type"]
    cmd = res["commands"][0]
    assert cmd["title"] == "Пересчитать всё"
    assert cmd["handler"] == "Пересчитать" and cmd["declared"] is True
    items = {i["name"]: i for i in res["items"]}
    assert items["Артикул"]["data_path"] == "Объект.Артикул"
    assert items["Артикул"]["handlers"][0]["handler"] == "АртикулПриИзменении"
    assert items["Артикул"]["handlers"][0]["declared"] is True
    assert items["КнопкаПересчитать"]["command"] == "Пересчитать"
    assert items["Флаг"]["path"] == "Группа"
    fh = res["form_handlers"][0]
    assert fh["event"] == "OnOpen" and fh["handler"] == "ПриОткрытии" and fh["declared"] is True
