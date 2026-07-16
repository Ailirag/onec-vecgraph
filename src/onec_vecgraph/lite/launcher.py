"""One-word launcher for onec-lite — the simplest possible start.

    onec-lite                    stdio MCP-сервер (регистрация в Claude Code/Cursor)
    onec-lite admin              http + веб-админка, браузер откроется сам (:8010/admin)
    onec-lite check              напечатать источники/справку и выйти

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="onec-lite",
        description="MCP-сервер по живой рабочей копии 1С (без Neo4j и векторов).",
        epilog="Первая настройка: onec-lite admin (пути задаются в браузере и сохраняются).",
    )
    parser.add_argument("mode", nargs="?", choices=("stdio", "admin", "check"), default="stdio",
                        help="stdio (по умолчанию, для MCP-клиентов) | admin (веб-админка) | check")
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
