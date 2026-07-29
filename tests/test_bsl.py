from onec_vecgraph.bsl.parser import parse_module

SRC = """
#Область Главное
&НаСервере
Процедура Запустить() Экспорт
    Помощник();
    ОбщийМодуль.Сделать(Параметр);
    // это комментарий ЛишнийВызов()
    Текст = "строка с ВызовВСтроке()";
КонецПроцедуры
#КонецОбласти

Функция Помощник()
    Возврат Истина;
КонецФункции
"""


def test_parse_module_extracts_routines_and_calls() -> None:
    routines = {r.name: r for r in parse_module(SRC)}
    assert set(routines) == {"Запустить", "Помощник"}

    run = routines["Запустить"]
    assert run.kind == "Procedure"
    assert run.export is True
    assert run.region == "Главное"
    assert run.directive == "НаСервере"
    assert routines["Помощник"].directive is None

    calls = {(c.qualifier, c.method) for c in run.calls}
    assert (None, "Помощник") in calls            # local call
    assert ("ОбщийМодуль", "Сделать") in calls    # qualified (common module) call
    # comments and string literals must not produce calls
    assert all(c.method not in ("ЛишнийВызов", "ВызовВСтроке") for c in run.calls)

    helper = routines["Помощник"]
    assert helper.kind == "Function"
    assert helper.export is False


_OVERRIDE_SRC = """
&НаСервере
&Вместо("ПередЗаписью")
Процедура Расш_ПередЗаписью(Отказ)
    ПроверитьЛимит();
КонецПроцедуры

&После("ОбработкаПроведения")
Процедура Расш_ОбработкаПроведения(Отказ, Режим)
КонецПроцедуры

Процедура Обычная()
КонецПроцедуры
"""


def test_parse_module_captures_override_annotations() -> None:
    r = {x.name: x for x in parse_module(_OVERRIDE_SRC)}

    # &Вместо("ПередЗаписью") with a compilation directive alongside: both kept.
    rep = r["Расш_ПередЗаписью"]
    assert rep.override_mode == "Вместо"
    assert rep.override_target == "ПередЗаписью"
    assert rep.directive == "НаСервере"

    aft = r["Расш_ОбработкаПроведения"]
    assert aft.override_mode == "После" and aft.override_target == "ОбработкаПроведения"

    # An ordinary routine carries no override annotation.
    assert r["Обычная"].override_mode is None and r["Обычная"].override_target is None


def test_export_on_multiline_signature() -> None:
    """`Экспорт` стоит после ЗАКРЫВАЮЩЕЙ скобки, а параметры в 1С часто переносят на строки.

    Поиск только по строке объявления помечал такие рутины неэкспортными — на живой УТ так
    врал каждый 25-й экспортный метод: find_routine(exported_only=True) их не находил, а
    review_set недооценивал риск (export — первый ключ ранжирования)."""
    src = (
        "Функция Многострочная(Знач Первый,\n"
        "    Знач Второй,\n"
        "    Знач Третий) Экспорт\n"
        "    Возврат 1;\n"
        "КонецФункции\n"
        "\n"
        "Процедура БезЭкспорта(А)\n"
        "    Возврат;\n"
        "КонецПроцедуры\n"
    )
    routines = {x.name: x for x in parse_module(src)}
    assert routines["Многострочная"].export is True
    assert routines["БезЭкспорта"].export is False
