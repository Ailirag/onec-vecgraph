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
