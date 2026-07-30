"""Общая изоляция тестов от РЕАЛЬНОГО окружения пользователя.

Состояние и индексы onec-lite по умолчанию живут в `~/.onec-lite/`. Тесты, которые не задали
`ONEC_LITE_STATE` сами (например, с сессионной фикстурой на `tmp_path_factory`), писали БД
индекса прямо в домашний каталог: там оставались файлы вида `<хэш>.db` с 13 рутинами от
временных репозиториев pytest. Мало того что это мусор в чужом каталоге — такой файл легко
принять за состояние продакшена при разборе «почему индекс пуст».

Барьер один на всю сессию и не мешает тестам, которые переопределяют те же переменные
функциональным monkeypatch: их значение перекрывает сессионное.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_onec_lite_home(tmp_path_factory: pytest.TempPathFactory):
    home = tmp_path_factory.mktemp("onec_lite_home")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("ONEC_LITE_STATE", str(home / "config.json"))
        mp.setenv("ONEC_LITE_FTS_DIR", str(home / "fts"))
        yield home


@pytest.fixture(autouse=True)
def _no_index_writes_into_real_home() -> None:
    """Страховка от регресса: путь индексов обязан лежать вне домашнего каталога.

    Проверяем в каждом тесте, а не единожды: любая фикстура может переопределить переменные."""
    from onec_vecgraph.lite import fts

    real = (Path.home() / ".onec-lite").resolve()
    target = fts.index_dir().resolve()
    assert not target.is_relative_to(real), (
        f"тест пишет индексы в реальный домашний каталог: {target}")
