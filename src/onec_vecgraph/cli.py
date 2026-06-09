"""Command-line interface: serve the MCP server, check health, index a dump."""

from __future__ import annotations

import sys

import typer
from rich.console import Console

from . import __version__
from .config import get_settings

# Make CLI output robust on legacy Windows code pages (e.g. cp1251 can't encode '▸').
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

_console = Console(legacy_windows=False, soft_wrap=True)


def rprint(*args: object, **kwargs: object) -> None:
    _console.print(*args, **kwargs)

app = typer.Typer(
    add_completion=False,
    help="onec-vecgraph — 1C config vectorization + dependency/call graph over Neo4j (MCP).",
)


def _flush_exit() -> None:
    """Force a clean process exit.

    torch/CUDA on Windows can hang at interpreter shutdown after heavy GPU use,
    leaving the process alive indefinitely. All DB writes are already committed, so
    exit immediately once results are printed.
    """
    import os
    import sys

    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


@app.command()
def version() -> None:
    """Print the version."""
    rprint(f"onec-vecgraph {__version__}")


@app.command()
def health() -> None:
    """Check Neo4j connectivity."""
    from .storage import Neo4jStore

    settings = get_settings()
    try:
        with Neo4jStore.from_settings(settings) as store:
            rprint(store.health())
    except Exception as exc:  # noqa: BLE001 - surface a friendly message to the CLI user
        rprint(f"[red]Neo4j connection failed:[/] {exc}")
        raise typer.Exit(code=1)


@app.command()
def serve(
    transport: str = typer.Option("http", help="Transport: 'http' (streamable) or 'stdio'."),
) -> None:
    """Run the MCP server."""
    from .server import run

    if transport == "http":
        settings = get_settings()
        rprint(
            f"[green]Starting MCP server[/] (streamable-http) on "
            f"http://{settings.mcp_host}:{settings.mcp_port}{settings.mcp_path}"
        )
        run("streamable-http")
    elif transport == "stdio":
        run("stdio")
    else:
        raise typer.BadParameter("transport must be 'http' or 'stdio'")


@app.command()
def index(
    path: str = typer.Argument(..., help="Path to the 1C Configurator XML dump directory."),
    tenant_id: str = typer.Option("default", help="Tenant (company) id."),
    reset: bool = typer.Option(False, help="Delete this tenant's graph before loading."),
    incremental: bool = typer.Option(
        False, help="Reindex only objects changed since last index (by ConfigDumpInfo hashes)."
    ),
) -> None:
    """Index a 1C Configurator XML dump (base + extensions) into Neo4j."""
    from .indexer import index_dump

    settings = get_settings()
    result = index_dump(path, tenant_id=tenant_id, settings=settings, reset=reset, incremental=incremental)
    rprint(f"[green]Indexed[/] ({result['mode']}) tenant={tenant_id!r} from {path!r}")
    for part in result["parts"]:
        tag = "extension" if part["extension"] else "base"
        rprint(f"  • {part['config_id']} ({tag}) — {part['name']!r} purpose={part['purpose']}")
    if result["mode"] == "incremental":
        rprint(f"  objects: total={result['objects_total']} changed={result['changed']} "
               f"deleted={result['deleted']} unchanged={result['unchanged']}")
    else:
        rprint(f"  object files seen: {result['object_files_seen']}  parsed: {result['objects_parsed']}")
    if result["parse_errors"]:
        rprint(f"  [red]parse errors: {result['parse_errors']}[/]")
        for err in result["parse_error_sample"]:
            rprint(f"    {err}")
    rprint(f"  written: {result['written']}")
    rprint(f"  by label: {result['counts']['by_label']}")
    rprint(f"  real objects: {result['counts']['real_objects']}  stubs: {result['counts']['stub_objects']}")


@app.command()
def ls(
    kind: str = typer.Option(None, help="Filter by object kind (Catalog, Enum, ...)."),
    name: str = typer.Option(None, help="Substring of name/synonym."),
    tenant_id: str = typer.Option("default"),
    limit: int = typer.Option(100),
) -> None:
    """List metadata objects."""
    from . import queries
    from .storage import Neo4jStore

    with Neo4jStore.from_settings(get_settings()) as store:
        for row in queries.list_metadata(store, tenant_id, kind, name, limit):
            rprint(f"{row['fqn']}  —  {row['synonym']}  [{row['config_id']}]")


@app.command()
def show(
    query: str = typer.Argument(..., help="Object fqn (Catalog.X) or name."),
    tenant_id: str = typer.Option("default"),
    detail: bool = typer.Option(False, "--detail", help="Include the full raw property set."),
) -> None:
    """Show an object card (attributes, types, tabular sections, enum values, ...)."""
    from . import queries
    from .storage import Neo4jStore

    with Neo4jStore.from_settings(get_settings()) as store:
        rprint(queries.get_object(store, tenant_id, query, detail=detail))


@app.command()
def deps(
    query: str = typer.Argument(..., help="Object fqn or name."),
    direction: str = typer.Option("both", help="out | in | both"),
    tenant_id: str = typer.Option("default"),
) -> None:
    """Show dependencies of an object."""
    from . import queries
    from .storage import Neo4jStore

    with Neo4jStore.from_settings(get_settings()) as store:
        rprint(queries.get_dependencies(store, tenant_id, query, direction))


@app.command()
def usages(
    query: str = typer.Argument(..., help="Object fqn or name used as a reference type."),
    tenant_id: str = typer.Option("default"),
) -> None:
    """Find where an object is used as a reference type (attributes/dimensions)."""
    from . import queries
    from .storage import Neo4jStore

    with Neo4jStore.from_settings(get_settings()) as store:
        rprint(queries.find_type_usages(store, tenant_id, query))


@app.command()
def vectorize(
    tenant_id: str = typer.Option("default", help="Tenant whose graph to vectorize."),
    no_reset: bool = typer.Option(False, help="Keep existing chunks instead of rebuilding."),
    incremental: bool = typer.Option(
        False, help="Re-embed only objects whose configVersion changed since last vectorize."
    ),
    code: bool = typer.Option(
        False, help="Also vectorize BSL code (per-routine chunks of object/common/form modules)."
    ),
) -> None:
    """Build embeddings for a tenant's metadata (chunks + vector/full-text indexes)."""
    from .vectorizer import vectorize as run_vectorize

    result = run_vectorize(tenant_id, get_settings(), reset=not no_reset, incremental=incremental, code=code)
    rprint(result)
    _flush_exit()


@app.command()
def search(
    query: str = typer.Argument(..., help="Natural-language or identifier query."),
    tenant_id: str = typer.Option("default"),
    mode: str = typer.Option("hybrid", help="hybrid | semantic"),
    top_k: int = typer.Option(10),
    kind: list[str] = typer.Option(None, help="Filter by object kind (repeatable): Catalog, Document, Subsystem..."),
    chunk_kind: list[str] = typer.Option(None, help="Filter by chunk kind (repeatable): object, code, attribute, form..."),
    subsystem: str = typer.Option(None, help="Restrict to a subsystem (name/fqn) and its descendants."),
    source: list[str] = typer.Option(None, help="Filter by corpus (repeatable): config, its, artifact."),
    expand: bool = typer.Option(False, help="Attach a compact graph neighborhood (GraphRAG) to each hit."),
) -> None:
    """Search indexed corpora (semantic or hybrid), with optional filters."""
    from . import queries
    from .embeddings.runtime import provider, reranker
    from .storage import Neo4jStore

    settings = get_settings()
    embedder = provider(settings)
    f = dict(kinds=kind or None, chunk_kinds=chunk_kind or None, subsystem=subsystem,
             source=source or None, expand=expand)
    with Neo4jStore.from_settings(settings) as store:
        if mode == "semantic":
            rprint(queries.semantic_search(store, tenant_id, query, embedder, top_k, **f))
        else:
            rprint(queries.hybrid_search(store, tenant_id, query, embedder, top_k,
                                         reranker=reranker(settings), **f))
    _flush_exit()


@app.command()
def metrics(
    tenant_id: str = typer.Option("default"),
    subsystem: str = typer.Option(None, help="Scope to a subsystem (name/fqn) and its descendants."),
) -> None:
    """Inventory & hotspot metrics for a tenant (optionally scoped to a subsystem)."""
    from . import queries
    from .storage import Neo4jStore

    with Neo4jStore.from_settings(get_settings()) as store:
        rprint(queries.metrics(store, tenant_id, subsystem))


@app.command()
def ingest(
    manifest: str = typer.Argument(..., help="Path to a sources manifest (YAML/JSON)."),
    tenant_id: str = typer.Option(None, help="Tenant id (overrides manifest 'tenant')."),
    only: str = typer.Option(None, help="Ingest only this source type: config_dump | its | git_artifacts."),
    reset: bool = typer.Option(False, help="Rebuild corpora from scratch (default: incremental by version_hash)."),
    link_semantic: bool = typer.Option(False, help="Also create RELATES_TO edges via nearest config objects."),
) -> None:
    """Ingest configured sources (ITS / project artifacts / config) for a tenant, per a manifest."""
    from .ingest import ingest_manifest

    rprint(ingest_manifest(manifest, get_settings(), tenant_id=tenant_id, only_type=only,
                           reset=reset, link_semantic=link_semantic))
    _flush_exit()


@app.command()
def callgraph(
    tenant_id: str = typer.Option("default", help="Tenant whose BSL modules to analyze."),
    no_reset: bool = typer.Option(False, help="Keep existing routines instead of rebuilding."),
    incremental: bool = typer.Option(
        False, help="Reparse only objects whose configVersion changed (falls back to full if a common module changed)."
    ),
) -> None:
    """Build the BSL call graph (routines + CALLS edges) for a tenant."""
    from .callgrapher import build_call_graph

    rprint(build_call_graph(tenant_id, get_settings(), reset=not no_reset, incremental=incremental))


@app.command()
def callers(query: str, tenant_id: str = typer.Option("default")) -> None:
    """Who calls a routine (fqn | 'Module.Method' | name)."""
    from . import queries
    from .storage import Neo4jStore

    with Neo4jStore.from_settings(get_settings()) as store:
        rprint(queries.find_callers(store, tenant_id, query))


@app.command()
def callees(query: str, tenant_id: str = typer.Option("default")) -> None:
    """What a routine calls."""
    from . import queries
    from .storage import Neo4jStore

    with Neo4jStore.from_settings(get_settings()) as store:
        rprint(queries.find_callees(store, tenant_id, query))


@app.command()
def path(
    from_routine: str = typer.Argument(...),
    to_routine: str = typer.Argument(...),
    tenant_id: str = typer.Option("default"),
) -> None:
    """Shortest BSL call path between two routines."""
    from . import queries
    from .storage import Neo4jStore

    with Neo4jStore.from_settings(get_settings()) as store:
        rprint(queries.call_path(store, tenant_id, from_routine, to_routine))


@app.command()
def handlers(
    query: str = typer.Argument(..., help="Object fqn or name."),
    tenant_id: str = typer.Option("default"),
) -> None:
    """Behavior entry points of an object: form event handlers + standard module events."""
    from . import queries
    from .storage import Neo4jStore

    with Neo4jStore.from_settings(get_settings()) as store:
        rprint(queries.find_handlers(store, tenant_id, query))


@app.command()
def snapshot(
    path: str = typer.Argument(..., help="Path to the Configurator XML dump."),
    out: str = typer.Option(None, help="Output JSON path (default: snapshots/<name>_<ts>.json)."),
) -> None:
    """Save a configVersion snapshot (per-object hashes) for before/after comparison."""
    import json
    from datetime import datetime
    from pathlib import Path as _P

    from .parsing import discover_parts
    from .parsing.dumpinfo import parse_dump_info

    root = _P(path)
    parts = discover_parts(root)
    versions: dict[str, str] = {}
    for part in parts:
        for fqn, ver in parse_dump_info(_P(part.root_dir)).items():
            versions[f"{part.config_id}|{fqn}"] = ver
    data = {
        "path": str(root),
        "taken_at": datetime.now().isoformat(timespec="seconds"),
        "parts": [{"config_id": p.config_id, "name": p.name, "extension": p.is_extension} for p in parts],
        "object_count": len(versions),
        "versions": versions,
    }
    if out is None:
        _P("snapshots").mkdir(exist_ok=True)
        out = str(_P("snapshots") / f"{root.name}_{datetime.now():%Y%m%d_%H%M%S}.json")
    _P(out).parent.mkdir(parents=True, exist_ok=True)
    _P(out).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    rprint(f"[green]Snapshot saved[/]: {out}")
    rprint(f"  parts: {[p['config_id'] for p in data['parts']]}  objects with configVersion: {len(versions)}")


@app.command(name="snapshot-diff")
def snapshot_diff(
    before: str = typer.Argument(..., help="Earlier snapshot JSON."),
    after: str = typer.Argument(..., help="Later snapshot JSON."),
    limit: int = typer.Option(50, help="Max items to list per category."),
) -> None:
    """Compare two configVersion snapshots: changed / added / removed objects."""
    import json
    from pathlib import Path as _P

    b = json.loads(_P(before).read_text(encoding="utf-8"))["versions"]
    a = json.loads(_P(after).read_text(encoding="utf-8"))["versions"]
    bk, ak = set(b), set(a)
    changed = sorted(k for k in (ak & bk) if a[k] != b[k])
    added = sorted(ak - bk)
    removed = sorted(bk - ak)
    rprint(f"[bold]changed={len(changed)}  added={len(added)}  removed={len(removed)}[/]")
    for label, items in (("changed", changed), ("added", added), ("removed", removed)):
        for k in items[:limit]:
            rprint(f"  [{label}] {k}")
        if len(items) > limit:
            rprint(f"  …(+{len(items) - limit} more {label})")


if __name__ == "__main__":
    app()
