"""One-word launcher for onec-lite — the simplest possible start.

    onec-lite                    stdio MCP-сервер (регистрация в Claude Code/Cursor)
    onec-lite admin              http + веб-админка, браузер откроется сам (:8010/admin)
    onec-lite check              напечатать источники/справку и выйти
    onec-lite update [--pull]    обновить воркспейс из remote (fetch; --pull = ff-pull)
    onec-lite sync ...           периодически обновлять ВСЕ воркспейсы (--interval N / --at HH:MM / --once)

Пути берутся из --root/--ext-root/--help-path, env ONEC_LITE_*, либо из состояния,
сохранённого админкой (~/.onec-lite/config.json) — поэтому после первой настройки
через админку запуск не требует ни одного аргумента.

Намеренно без typer/rich: мгновенный импорт и ни байта лишнего вывода в stdio-режиме
(stdout там принадлежит MCP-протоколу).
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser


def _apply_env(args: argparse.Namespace) -> None:
    """CLI-аргументы становятся env: lite-сервер читает конфигурацию оттуда."""
    if args.root:
        os.environ["ONEC_LITE_ROOT"] = args.root
    if args.workspace:
        os.environ["ONEC_LITE_WORKSPACE"] = args.workspace
    if args.ext_root:
        os.environ["ONEC_LITE_EXT_ROOTS"] = ";".join(args.ext_root)
    if args.help_path:
        os.environ["ONEC_LITE_HELP"] = ";".join(args.help_path)
    if args.host:
        os.environ["ONEC_LITE_HOST"] = args.host
    if args.port:
        os.environ["ONEC_LITE_PORT"] = str(args.port)
    if args.mode == "admin":
        os.environ["ONEC_LITE_ADMIN"] = "true"


def _url() -> str:
    host = os.environ.get("ONEC_LITE_HOST", "").strip() or "127.0.0.1"
    port = os.environ.get("ONEC_LITE_PORT", "").strip() or "8010"
    return f"http://{host}:{port}"


def _check() -> int:
    from . import server as lite_server

    try:
        ws = lite_server._ws()  # noqa: SLF001 - ленивый резолв env/state
    except RuntimeError as exc:
        print(f"{exc}\nПодсказка: onec-lite admin — задать пути в браузере.")
        return 1
    print(f"Воркспейс: {lite_server.default_workspace_name()}")
    print(f"Рабочая копия: {ws.root}")
    for s in ws.sources:
        counts = ws.kind_counts(s)
        tag = "расширение" if s.is_extension else "база"
        print(f"  {s.name} ({s.fmt}, {tag}) — {sum(counts.values())} объектов, видов: {len(counts)}")
    hv = lite_server.help_catalog().versions()
    for v in hv["versions"]:
        print(f"  справка платформы {v['platform_version']} — файлов .hbk: {v['files']}")
    return 0


def _update(pull: bool) -> int:
    """Обновить дефолтный воркспейс из remote: зеркало → clone/pull, путь → fetch|pull."""
    from . import admin as lite_admin
    from . import gitops
    from . import server as lite_server

    name = lite_server.default_workspace_name()
    wss, _active = lite_admin.load_workspaces(lite_admin.state_file())
    entry = wss.get(name)
    if entry is None:
        print(f"Воркспейс '{name}' не найден. Известные: {', '.join(sorted(wss)) or '(нет)'}")
        return 1
    res = gitops.update_workspace(name, entry, mode="pull" if pull else "fetch")
    label = res.get("op", "update")
    if not res.get("ok"):
        print(f"{name}: {label} — ОШИБКА: {res.get('error')}")
        return 1
    extra = []
    if res.get("branch"):
        extra.append(f"ветка {res['branch']}")
    if res.get("behind"):
        extra.append(f"отстаёт на {res['behind']} (сделайте --pull)")
    print(f"{name}: {label} — ok. {res.get('output') or ''} {'· ' + ', '.join(extra) if extra else ''}".strip())
    return 0


def _parse_at_times(vals: list[str]) -> list[tuple[int, int]] | None:
    """'HH:MM' → (час, минута); None при ошибке разбора (сообщение уже напечатано)."""
    out: list[tuple[int, int]] = []
    for v in vals:
        parts = (v or "").strip().split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            print(f"onec-lite sync: неверное время --at {v!r} (ожидается HH:MM)")
            return None
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h < 24 and 0 <= m < 60):
            print(f"onec-lite sync: время вне диапазона --at {v!r}")
            return None
        out.append((h, m))
    return out


def _refresh_index_after_pull(name: str, entry: dict, logger) -> None:
    """Догнать индекс после обновления рабочей копии.

    Сам git-проход только приносит файлы. Индекс инкрементальный, но кикается ЛЕНИВО — из
    поиска (не чаще раза в 30 с) и при обнаружении отставания в запросе вызывающих. Значит
    после ночного sync первые запросы платили живым разбором всего подтянутого, а до первого
    поиска индекс мог оставаться на коммите прошлой недели. Здесь догон делается сразу: это
    обслуживающий проход, ждать его уместно. Кросс-процессный лок не даёт встать вторым
    писателем рядом с работающим сервером."""
    from . import fts as lite_fts
    from .workspace import Workspace

    root = str(entry.get("root") or "").strip()
    if not root:
        return
    try:
        ws = Workspace(root, ext_roots=tuple(entry.get("ext_roots") or ()))
        res = lite_fts.index_for(ws).build()
    except Exception as exc:  # noqa: BLE001 — догон не должен ронять проход синка
        logger.warning("%s: догон индекса не удался: %s: %s", name, type(exc).__name__, exc)
        return
    if err := res.get("error"):
        logger.warning("%s: догон индекса: %s", name, err)
        return
    changed = sum(int(res.get(k) or 0) for k in ("files_added", "files_updated", "files_removed"))
    logger.info("%s: индекс догнан — файлов затронуто %d, за %s с", name, changed,
                res.get("seconds", "?"))


def _sync_pass(pull: bool, only: set[str] | None, logger, *, refresh_index: bool = True,
               ) -> tuple[int, int]:
    """Один проход по ВСЕМ воркспейсам: зеркало → clone/pull, путь → fetch (или ff-pull при --pull),
    затем догон индекса по подтянутым файлам.
    Возвращает (обновлено, ошибок). Ошибка отдельного репозитория не прерывает проход:
    gitops.update_workspace сама не бросает исключений (см. её докстринг)."""
    from . import admin as lite_admin
    from . import gitops

    wss, _active = lite_admin.load_workspaces(lite_admin.state_file())
    if not wss:
        logger.warning("нет ни одного воркспейса в %s", lite_admin.state_file())
        return 0, 0
    ok = fail = 0
    for name, entry in sorted(wss.items()):
        if only and name not in only:
            continue
        try:
            res = gitops.update_workspace(name, entry, mode="pull" if pull else "")
        except Exception as exc:  # noqa: BLE001 — доп. страховка поверх и без того безопасной update_workspace
            res = {"ok": False, "op": "sync", "error": f"{type(exc).__name__}: {exc}"}
        op = res.get("op", "update")
        if res.get("ok"):
            ok += 1
            out = (res.get("output") or "").strip().splitlines()
            logger.info("%s: %s — ok%s", name, op, (" · " + out[0]) if out else "")
            if refresh_index:
                _refresh_index_after_pull(name, entry, logger)
        else:
            fail += 1
            logger.warning("%s: %s — ОШИБКА: %s", name, op, res.get("error"))
    return ok, fail


def _sync(interval: int | None, at_raw: list[str], once: bool, pull: bool,
          only: set[str] | None, refresh_index: bool = True) -> int:
    """Демон обновления воркспейсов. stdlib-планировщик: --interval N (сек, дрейфонезависимо
    через monotonic) или --at HH:MM (фиксированное время суток; приоритетнее interval).
    --once — один проход и выход. Логи — в stderr (переживают nohup/systemd).
    После каждого успешного обновления индекс догоняется по подтянутым файлам
    (--no-index-refresh отключает: тогда догон останется ленивым, из запросов)."""
    import logging
    import time
    from datetime import datetime, timedelta

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s onec-lite sync: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    logger = logging.getLogger("onec_vecgraph.lite.sync")

    at_times = _parse_at_times(at_raw)
    if at_times is None:
        return 2
    if not once and not interval and not at_times:
        print("onec-lite sync: укажите --interval N (сек) и/или --at HH:MM, либо --once")
        return 2
    if at_times and interval:
        logger.warning("--interval игнорируется: заданы фиксированные времена --at")

    def one_pass() -> None:
        ok, fail = _sync_pass(pull, only, logger, refresh_index=refresh_index)
        logger.info("проход завершён: обновлено %d, ошибок %d", ok, fail)

    if once:
        one_pass()
        return 0
    if not at_times:
        one_pass()   # чистый --interval: первый проход сразу; при --at ждём назначенное время суток

    next_tick = time.monotonic()
    while True:
        if at_times:
            now = datetime.now()
            cands = []
            for (h, m) in at_times:
                t = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if t <= now:
                    t += timedelta(days=1)
                cands.append(t)
            delay = max(1.0, (min(cands) - now).total_seconds())
        else:
            next_tick += max(1, interval or 0)
            delay = max(0.0, next_tick - time.monotonic())
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            logger.info("остановлен (Ctrl+C)")
            return 0
        one_pass()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="onec-lite",
        description="MCP-сервер по живой рабочей копии 1С (без Neo4j и векторов).",
        epilog="Первая настройка: onec-lite admin (пути задаются в браузере и сохраняются).",
    )
    parser.add_argument("mode", nargs="?", choices=("stdio", "admin", "check", "update", "sync"),
                        default="stdio",
                        help="stdio (по умолчанию, для MCP-клиентов) | admin (веб-админка) | "
                             "check | update (обновить воркспейс из remote) | "
                             "sync (демон обновления всех воркспейсов по расписанию)")
    parser.add_argument("--pull", action="store_true",
                        help="update/sync: pull --ff-only вместо безопасного fetch (для path-воркспейсов; "
                             "зеркала обновляются в любом случае)")
    parser.add_argument("--interval", type=int, metavar="N",
                        help="sync: период между проходами в секундах (напр. 3600)")
    parser.add_argument("--at", action="append", default=[], metavar="HH:MM",
                        help="sync: фиксированное время суток для прохода (повторяемый; напр. --at 08:00 --at 14:00). "
                             "Заданные --at имеют приоритет над --interval")
    parser.add_argument("--once", action="store_true",
                        help="sync: один проход и выход (для внешнего планировщика cron/systemd)")
    parser.add_argument("--no-index-refresh", action="store_true",
                        help="sync: НЕ догонять индекс после обновления (по умолчанию догоняем: иначе подтянутые файлы разбираются живьём на каждом запросе, пока кто-нибудь не выполнит поиск)")
    parser.add_argument("--root", help="корень рабочей копии (Конфигуратор XML или EDT)")
    parser.add_argument("--workspace",
                        help="имя воркспейса: дефолт этой сессии (с --root — имя, под которым "
                             "корень будет сконфигурирован); сервер может держать несколько")
    parser.add_argument("--ext-root", action="append", default=[],
                        help="дополнительный корень расширения (повторяемый)")
    parser.add_argument("--help-path", action="append", default=[],
                        help="справка платформы: каталог bin / .hbk / «версия=путь» (повторяемый)")
    parser.add_argument("--host", help="HTTP-хост (по умолчанию 127.0.0.1)")
    parser.add_argument("--port", type=int, help="HTTP-порт (по умолчанию 8010)")
    parser.add_argument("--no-browser", action="store_true",
                        help="admin: не открывать браузер автоматически")
    args = parser.parse_args(argv)
    _apply_env(args)

    if args.mode == "check":
        return _check()

    if args.mode == "update":
        return _update(pull=args.pull)

    if args.mode == "sync":
        only = {args.workspace} if args.workspace else None
        return _sync(refresh_index=not args.no_index_refresh,
                     interval=args.interval, at_raw=args.at, once=args.once,
                     pull=args.pull, only=only)

    from . import server as lite_server

    if args.mode == "admin":
        url = _url()
        print(f"onec-lite админка:  {url}/admin")
        print(f"MCP-эндпоинт:       {url}/mcp   (остановка: Ctrl+C)")
        if not args.no_browser:
            threading.Timer(1.2, webbrowser.open, args=(f"{url}/admin",)).start()
        lite_server.run("streamable-http")
        return 0

    lite_server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
