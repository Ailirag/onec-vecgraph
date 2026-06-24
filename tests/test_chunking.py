from types import SimpleNamespace

from onec_vecgraph import chunking


def _rt(name, kind="Procedure", directive=None):
    return SimpleNamespace(name=name, kind=kind, directive=directive)


def _ctx(**over):
    base = dict(owner_kind="Document", owner_name="РеализацияТоваров", owner_syn="Реализация товаров",
                owner_fqn="Document.РеализацияТоваров", module_fqn="Document.РеализацияТоваров.Module.ObjectModule",
                module_type="ObjectModule", config_id="base", config_version="v1", handlers={})
    base.update(over)
    return base


def test_classify_entry_point() -> None:
    assert chunking.classify_entry_point("ОбработкаПроведения") == "проведение"
    assert chunking.classify_entry_point("ПроверкаЗаполнения") == "проверка_заполнения"
    assert chunking.classify_entry_point("ПриЗаписи") == "запись"
    assert chunking.classify_entry_point("ПростаяФункция") is None
    assert chunking.classify_entry_point("ИмяОбработчика", form_event="ПриИзменении") == "событие_формы"


def test_code_chunks_tags_entry_point_and_keeps_short_handlers() -> None:
    # Short routine that is an entry point must be kept (below the boilerplate threshold).
    chunks = chunking.code_chunks(_rt("ОбработкаПроведения"),
                                  "Процедура ОбработкаПроведения(Отказ, Режим)\n Возврат;\nКонецПроцедуры", _ctx())
    assert len(chunks) == 1
    assert chunks[0].entry_point == "проведение"
    assert "[точка входа: проведение]" in chunks[0].text
    assert chunks[0].chunk_kind == "code"


def test_code_chunks_drops_short_non_entry_routine() -> None:
    assert chunking.code_chunks(_rt("Мелочь"), "Процедура Мелочь()\nКонецПроцедуры", _ctx()) == []


def test_code_chunks_marks_extension_override() -> None:
    rt = SimpleNamespace(name="Расш_ПередЗаписью", kind="Procedure", directive="НаСервере",
                         override_mode="Вместо", override_target="ПередЗаписью")
    ctx = _ctx(module_config_id="ext:ДИТ_РасширениеАдаптацияУТ",
               module_fqn="Document.РеализацияТоваров.Module.ObjectModule@ext:ДИТ_РасширениеАдаптацияУТ")
    body = ("Процедура Расш_ПередЗаписью(Отказ)\n    ПроверитьЛимитКредита(Объект);\n"
            "    ЗаписатьВЖурналРегистрации(Объект.Ссылка);\n    ОбновитьСвязанныеДанные();\nКонецПроцедуры")
    chunks = chunking.code_chunks(rt, body, ctx)
    assert chunks, "override routine must produce a code chunk"
    text = chunks[0].text
    assert "расширение «ДИТ_РасширениеАдаптацияУТ»" in text  # provenance in the head
    assert "[Вместо «ПередЗаписью»]" in text                 # which base method it hooks


def test_code_chunks_splits_large_routine_without_truncation() -> None:
    body = "Процедура Большая()\n" + "\n".join(f"  Перем П{i} = Вычислить{i}();" for i in range(400)) + "\nКонецПроцедуры"
    chunks = chunking.code_chunks(_rt("Большая"), body, _ctx())
    assert len(chunks) > 1  # split into parts
    # every source line survives somewhere (no hard truncation)
    joined = "\n".join(c.text for c in chunks)
    assert "Вычислить399" in joined
    # distinct chunk fqns per part, all stripping to the same routine address
    fqns = {c.fqn for c in chunks}
    assert len(fqns) == len(chunks)
    assert all(f.split("#code")[0] == "Document.РеализацияТоваров.Module.ObjectModule::Большая" for f in fqns)


def test_chunk_source_defaults_to_config_and_is_in_props() -> None:
    c = chunking.object_chunk({"kind": "Catalog", "name": "X", "synonym": "Икс", "fqn": "Catalog.X"})
    assert c.source == "config"
    assert c.props()["source"] == "config"
    c2 = chunking.subsystem_chunk({"fqn": "Subsystem.S", "name": "S", "synonym": "S", "members": []})
    c2.source = "its"
    assert c2.props()["source"] == "its"


def test_subsystem_chunk_lists_composition() -> None:
    row = {"fqn": "Subsystem.Продажи", "name": "Продажи", "synonym": "Продажи", "comment": "Учёт продаж",
           "config_id": "base", "config_version": "v1",
           "members": [{"name": "РеализацияТоваров", "syn": "Реализация", "kind": "Document"}]}
    c = chunking.subsystem_chunk(row)
    assert c.chunk_kind == "subsystem"
    assert "Подсистема «Продажи»" in c.text
    assert "Состав:" in c.text and "Реализация" in c.text


def test_role_chunk_lists_rights() -> None:
    row = {"fqn": "Role.Продавец", "name": "Продавец", "synonym": "Продавец", "comment": "",
           "config_id": "base", "config_version": "v1",
           "rights": [{"name": "РеализацияТоваров", "syn": "Реализация", "granted": ["Read", "Insert"]}]}
    c = chunking.role_chunk(row)
    assert c.chunk_kind == "role"
    assert "Роль «Продавец»" in c.text
    assert "Реализация: Read, Insert" in c.text
