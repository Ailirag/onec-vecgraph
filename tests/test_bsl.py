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


def test_end_keyword_after_semicolon_does_not_swallow_next_routine() -> None:
    """`КонецЕсли;КонецПроцедуры` в одной строке — рутина обязана закрыться именно там.

    При якоре `^` конец не распознавался, рутина «продолжалась» до следующего КонецПроцедуры и
    ПОГЛОЩАЛА следующую целиком: та исчезала из list_routines/find_routine/индекса, а её вызовы
    приписывались чужой рутине. На УТ так терялась рутина формы документа."""
    src = (
        "Процедура Первая()\n"
        "    Если Истина Тогда\n"
        "        Сообщить(1);\n"
        "    КонецЕсли;КонецПроцедуры\n"
        "\n"
        "Процедура Вторая()\n"
        "    ЧужойВызов();\n"
        "КонецПроцедуры\n"
    )
    routines = {x.name: x for x in parse_module(src)}
    assert set(routines) == {"Первая", "Вторая"}
    assert routines["Первая"].end_line == 4
    assert [c.method for c in routines["Первая"].calls] == ["Сообщить"]
    assert [c.method for c in routines["Вторая"].calls] == ["ЧужойВызов"]


def test_commented_continuation_of_multiline_string() -> None:
    """Закомментированная часть многострочного литерала не должна ломать состояние очистки.

    В 1С между частями литерала допустим комментарий. Раньше кавычка ИЗ КОММЕНТАРИЯ закрывала
    литерал, следующая настоящая часть открывала его заново, и до конца файла всё гасилось в
    пробелы: парсер терял и `КонецПроцедуры`, и объявления. На УТ так исчезала рутина, а её тело
    доставалось предыдущей (26 рутин в модуле вместо 40)."""
    src = (
        "Процедура Первая()\n"
        '    Т = "Колонки:\n'
        '        //|СтароеПоле");\n'
        '        |НовоеПоле");\n'
        "    Сообщить(Т);\n"
        "КонецПроцедуры\n"
        "\n"
        "Функция Вторая() Экспорт\n"
        "    Возврат 1;\n"
        "КонецФункции\n"
    )
    routines = {x.name: x for x in parse_module(src)}
    assert set(routines) == {"Первая", "Вторая"}
    assert routines["Первая"].end_line == 6
    assert routines["Вторая"].export is True


def test_unterminated_string_does_not_eat_rest_of_file() -> None:
    """Незакрытый литерал обрывается на конце строки, а не съедает остаток модуля.

    Продолжение многострочной строки в 1С начинается с `|`; если следующая строка — обычный код,
    значит литерал не закрыт (битый источник). Гасить до следующей кавычки нельзя: так терялись
    рутины ниже."""
    src = (
        "Процедура Битая()\n"
        '    Т = "не закрыта\n'
        "    Сообщить(Т);\n"
        "КонецПроцедуры\n"
        "\n"
        "Процедура Целая()\n"
        "    ВидимыйВызов();\n"
        "КонецПроцедуры\n"
    )
    routines = {x.name: x for x in parse_module(src)}
    assert set(routines) == {"Битая", "Целая"}
    assert [c.method for c in routines["Целая"].calls] == ["ВидимыйВызов"]


def test_routine_declared_and_closed_on_one_line() -> None:
    """`Функция Х() Возврат 1; КонецФункции` — проверка конца стояла только в ветке «внутри
    рутины», поэтому такая рутина оставалась открытой и поглощала следующую целиком."""
    src = (
        "Функция Х() Возврат 1; КонецФункции\n"
        "\n"
        "Процедура Вторая()\n"
        "    ВидимыйВызов();\n"
        "КонецПроцедуры\n"
    )
    routines = {x.name: x for x in parse_module(src)}
    assert set(routines) == {"Х", "Вторая"}
    assert routines["Х"].end_line == 1
    assert [c.method for c in routines["Вторая"].calls] == ["ВидимыйВызов"]


def test_call_on_the_closing_line_is_not_lost() -> None:
    """Строка с `КонецПроцедуры` в тело не входила — вызов на ней был невидим для индекса."""
    src = (
        "Процедура Первая()\n"
        "    Если Истина Тогда Б(); КонецЕсли;КонецПроцедуры\n"
        "\n"
        "Процедура Вторая()\n"
        "    Возврат;\n"
        "КонецПроцедуры\n"
    )
    routines = {x.name: x for x in parse_module(src)}
    assert set(routines) == {"Первая", "Вторая"}
    assert [c.method for c in routines["Первая"].calls] == ["Б"]
