"""Пересобрать индексы воркспейсов, чья схема устарела (после правок парсера/схемы).

Инкремент по mtime такие изменения не подхватывает: файлы-то не менялись, изменился разбор.
Схема поднимается сознательно, а этот скрипт прогревает индексы заранее, чтобы первый вопрос
пользователя не платил полную сборку. Порядок — от мелких к крупным.

Запуск: uv run --no-sync python scripts/rebuild_stale_indexes.py [--only имя,имя]
"""

from __future__ import annotations

import argparse
import sys
import time

from onec_vecgraph.lite import admin as lite_admin
from onec_vecgraph.lite import fts
from onec_vecgraph.lite.workspace import Workspace


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="список воркспейсов через запятую")
    args = ap.parse_args()
    only = {x.strip() for x in args.only.split(",") if x.strip()}

    state = lite_admin.load_state(lite_admin.state_file())
    todo: list[tuple[str, Workspace]] = []
    for name, cfg in (state.get("workspaces") or {}).items():
        if only and name not in only:
            continue
        root = cfg.get("root") or cfg.get("path")
        if not root:
            continue
        try:
            ws = Workspace(root, ext_roots=tuple(cfg.get("ext_roots") or ()))
            st = fts.index_for(ws).status()
        except Exception as exc:  # noqa: BLE001
            print(f"{name}: пропуск — {exc}", flush=True)
            continue
        if st.get("schema_outdated") or not st.get("symbols"):
            todo.append((name, ws))
        else:
            print(f"{name}: уже на актуальной схеме ({st.get('symbols')} символов)", flush=True)

    def size(pair: tuple[str, Workspace]) -> int:
        try:
            return sum(len(pair[1].listing(s)) for s in pair[1].sources)
        except Exception:  # noqa: BLE001
            return 10**9

    todo.sort(key=size)
    print(f"к пересборке: {[n for n, _ in todo]}", flush=True)
    for name, ws in todo:
        t0 = time.time()
        try:
            fts.index_for(ws).build(wait=7200)
            st = fts.index_for(ws).status()
            print(f"{name}: готово за {round(time.time() - t0)} с, символов={st.get('symbols')}",
                  flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"{name}: ОШИБКА за {round(time.time() - t0)} с — {exc}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
