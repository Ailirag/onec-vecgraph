"""get_routine_source: reassembling routine body from its code chunks (pure join logic)."""

from onec_vecgraph.queries import _join_code_parts

# Code chunks carry a breadcrumb head line, then the code segment. Large routines split into
# `#code/N` parts; reassembly must order by N and drop each head line.
_MF = "CommonModule.X.Module.Module::Сделать"


def test_join_code_parts_orders_and_strips_head() -> None:
    rows = [
        {"fqn": f"{_MF}#code/1", "text": "Общий модуль «X» ▸ Функция Сделать (часть 2/2)\n    Возврат Итог;\nКонецФункции"},
        {"fqn": f"{_MF}#code/0", "text": "Общий модуль «X» ▸ Функция Сделать (часть 1/2)\nФункция Сделать()\n    Итог = 1;"},
    ]
    src = _join_code_parts(rows)
    assert src == "Функция Сделать()\n    Итог = 1;\n    Возврат Итог;\nКонецФункции"


def test_join_code_parts_single_unsplit_chunk() -> None:
    rows = [{"fqn": f"{_MF}#code", "text": "Общий модуль «X» ▸ Процедура Сделать\nПроцедура Сделать()\nКонецПроцедуры"}]
    assert _join_code_parts(rows) == "Процедура Сделать()\nКонецПроцедуры"
