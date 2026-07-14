"""Lite workspace: direct file-system access to a live working copy of a 1C configuration.

Serves the zero-infrastructure MCP server (`serve-lite`): no Neo4j, no embeddings — the
source of truth is the working copy itself (Configurator XML dump or 1C:EDT project),
re-read on demand. Reuses the project's format-aware readers for discovery and per-object
parsing; caches are mtime/TTL-validated so edits show up without a restart.

Vocabulary:
  * source — one configuration part as a named unit: the base configuration or one
    extension. Lookups without an explicit source go extension-first (extensions by
    name, then bases), mirroring how the platform resolves adopted objects.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from ..parsing import dump as _configurator
from ..parsing.detect import detect_format
from ..parsing.edt import reader as _edt
from ..parsing.model import ConfigPart, MetaObject

_READERS = {"configurator": _configurator, "edt": _edt}

# Working-copy listings/parses are revalidated after this many seconds (files change
# under our feet while the developer works; a parse costs milliseconds).
_LIST_TTL = 15.0

# Tool-facing module aliases -> .bsl file stem (both formats use the same stems).
MODULE_ALIASES = {
    "module": "Module",
    "object": "ObjectModule",
    "manager": "ManagerModule",
    "recordset": "RecordSetModule",
    "value": "ValueManagerModule",
    "command": "CommandModule",
}
_MODULE_STEMS = tuple(dict.fromkeys(MODULE_ALIASES.values())) + (
    "ManagedApplicationModule", "OrdinaryApplicationModule", "SessionModule",
    "ExternalConnectionModule",
)

#: (fqn, metadata file, object dir) — one row of a source listing (never parsed here).
ObjectRef = tuple[str, Path, Path]


@dataclass(frozen=True)
class LiteSource:
    """One configuration part exposed as a named source."""

    name: str
    fmt: str  # 'configurator' | 'edt'
    part: ConfigPart

    @property
    def is_extension(self) -> bool:
        return self.part.is_extension

    @property
    def root(self) -> Path:
        return Path(self.part.root_dir)

    @property
    def files_root(self) -> Path:
        """Directory the kind folders (Catalogs/, Documents/, ...) live in."""
        return self.root / "src" if self.fmt == "edt" else self.root


def _discover_root(root: Path) -> list[LiteSource]:
    fmt = detect_format(root)
    parts = _READERS[fmt].discover_parts(Path(root))
    return [LiteSource(name=p.name or Path(p.root_dir).name, fmt=fmt, part=p) for p in parts]


class Workspace:
    """A set of sources (base + extensions) with cached listings and object parsing."""

    def __init__(self, root: str | Path, ext_roots: tuple[str | Path, ...] = ()) -> None:
        self.root = Path(root)
        self.ext_roots = tuple(str(p) for p in ext_roots)  # as configured (admin prefill)
        found: list[LiteSource] = _discover_root(self.root)
        for extra in ext_roots:
            found.extend(_discover_root(Path(extra)))

        # Extension-first resolution order; de-duplicate names (last suffix wins a #N tag).
        found.sort(key=lambda s: (not s.is_extension, s.name.lower()))
        self.sources: list[LiteSource] = []
        seen: dict[str, int] = {}
        for s in found:
            n = seen.get(s.name.lower(), 0)
            seen[s.name.lower()] = n + 1
            self.sources.append(
                s if n == 0 else LiteSource(name=f"{s.name}#{n + 1}", fmt=s.fmt, part=s.part)
            )
        self._by_name = {s.name.lower(): s for s in self.sources}

        self._listing: dict[str, tuple[float, list[ObjectRef]]] = {}
        self._by_kind_name: dict[str, dict[tuple[str, str], ObjectRef]] = {}
        self._objects: dict[tuple[str, str], tuple[float, MetaObject]] = {}
        self._bsl: dict[str, tuple[float, list[Path]]] = {}

    # ------------------------------------------------------------------ sources

    def resolve_sources(self, source: str = "") -> tuple[list[LiteSource], str | None]:
        """Sources to traverse: all (extension-first) or a single one by name."""
        if not source:
            return self.sources, None
        s = self._by_name.get(source.lower())
        if s is None:
            names = ", ".join(x.name for x in self.sources)
            return [], f"Неизвестный источник '{source}'. Доступны: {names}."
        return [s], None

    def source_of_path(self, abs_path: Path) -> tuple[str, str]:
        """Map an absolute path back to (source name, path relative to its files_root)."""
        best: tuple[int, LiteSource] | None = None
        for s in self.sources:
            root = str(s.files_root)
            ap = str(abs_path)
            if ap.startswith(root) and (best is None or len(root) > best[0]):
                best = (len(root), s)
        if best is None:
            return "", str(abs_path).replace("\\", "/")
        rel = str(abs_path)[best[0] :].lstrip("\\/")
        return best[1].name, rel.replace("\\", "/")

    # ------------------------------------------------------------------ listings

    def listing(self, src: LiteSource, fresh: bool = False) -> list[ObjectRef]:
        """(fqn, meta file, object dir) rows of a source; cheap dir walk, TTL-cached.

        fresh=True всегда перечитывает ФС (нужно индексаторам, которым важны удаления)."""
        now = time.monotonic()
        hit = self._listing.get(src.name)
        if hit and not fresh and now - hit[0] < _LIST_TTL:
            return hit[1]
        if src.fmt == "edt":
            rows = _edt._iter_object_files(src.files_root)  # noqa: SLF001 - shared layout walker
        else:
            rows = _configurator._iter_object_files(src.files_root)  # noqa: SLF001
        self._listing[src.name] = (now, rows)
        self._by_kind_name[src.name] = {
            (fqn.split(".", 1)[0], fqn.split(".", 1)[1].lower()): (fqn, meta, obj_dir)
            for fqn, meta, obj_dir in rows
        }
        return rows

    def kind_counts(self, src: LiteSource) -> dict[str, int]:
        counts: dict[str, int] = {}
        for fqn, _meta, _dir in self.listing(src):
            kind = fqn.split(".", 1)[0]
            counts[kind] = counts.get(kind, 0) + 1
        return dict(sorted(counts.items()))

    def find_objects(
        self, kind: str, name: str, source: str = ""
    ) -> tuple[list[tuple[LiteSource, ObjectRef]], str | None]:
        """All sources holding kind+name, extension-first (adopted objects live in several).

        `name` for nested subsystems is the qualified tail ('Продажи.Subsystem.Розница');
        a plain dotted form ('Продажи.Розница') is normalized automatically.
        """
        sources, err = self.resolve_sources(source)
        if err:
            return [], err
        keys = [name.lower()]
        if kind == "Subsystem" and "." in name and ".subsystem." not in name.lower():
            keys.append(name.lower().replace(".", ".subsystem."))
        found: list[tuple[LiteSource, ObjectRef]] = []
        for s in sources:
            self.listing(s)
            for key in keys:
                ref = self._by_kind_name[s.name].get((kind, key))
                if ref:
                    found.append((s, ref))
                    break
        if not found:
            return [], (
                f"Объект {kind}.{name} не найден"
                + (f" в источнике '{source}'." if source else " ни в одном источнике.")
            )
        return found, None

    def find_object(
        self, kind: str, name: str, source: str = ""
    ) -> tuple[LiteSource | None, ObjectRef | None, list[str], str | None]:
        """First match by resolution order: (source, ref, also_in, error)."""
        found, err = self.find_objects(kind, name, source)
        if err:
            return None, None, [], err
        src, ref = found[0]
        return src, ref, [s.name for s, _ in found[1:]], None

    # ------------------------------------------------------------------ object parsing

    def parse_object(self, src: LiteSource, ref: ObjectRef) -> MetaObject:
        """Parse one object via the format reader; cached by the metadata file's mtime."""
        fqn, meta, obj_dir = ref
        key = (src.name, fqn)
        try:
            mtime = meta.stat().st_mtime
        except OSError:
            mtime = 0.0
        hit = self._objects.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
        reader = _READERS[src.fmt]
        parsed = reader.parse_objects(
            "lite", [src.part], [(fqn, meta, obj_dir, src.part.config_id, None)]
        )
        if not parsed.objects:
            raise ValueError(f"Не удалось разобрать {meta}: {'; '.join(parsed.errors) or 'пустой результат'}")
        obj = parsed.objects[0]
        self._objects[key] = (mtime, obj)
        return obj

    # ------------------------------------------------------------------ module/form paths

    def module_path(
        self, src: LiteSource, kind: str, name: str, module: str
    ) -> tuple[Path | None, str]:
        """Resolve a module alias/stem/'Form:<Имя>' to a .bsl path; ('', msg) lists what exists."""
        _fqn, _meta, obj_dir = self._require_ref(src, kind, name)
        if obj_dir is None:
            return None, f"Объект {kind}.{name} не найден в '{src.name}'."

        if module.startswith("Form:"):
            form = module[len("Form:") :]
            p = self.form_module_path(src, obj_dir, form)
            return (p, "") if p else (None, f"Модуль формы '{form}' не найден у {kind}.{name}.")

        stem = MODULE_ALIASES.get(module.lower(), module)
        if stem.endswith(".bsl"):
            stem = stem[: -len(".bsl")]
        p = self._module_file(src, obj_dir, stem)
        if p:
            return p, ""
        # CommonForm has no object module: its code is the form module in the object dir.
        if kind == "CommonForm":
            p = self.form_module_path(src, obj_dir.parent, obj_dir.name, common_form_dir=obj_dir)
            if p:
                return p, ""
        return None, (
            f"Модуль '{module}' не найден у {kind}.{name}. Доступны: "
            f"{', '.join(self.available_modules(src, obj_dir)) or '(нет .bsl)'}"
        )

    def _module_file(self, src: LiteSource, obj_dir: Path, stem: str) -> Path | None:
        base = obj_dir / "Ext" if src.fmt == "configurator" else obj_dir
        p = base / f"{stem}.bsl"
        return p if p.is_file() else None

    def form_module_path(
        self, src: LiteSource, obj_dir: Path, form: str, common_form_dir: Path | None = None
    ) -> Path | None:
        form_dir = common_form_dir if common_form_dir is not None else obj_dir / "Forms" / form
        p = (
            form_dir / "Ext" / "Form" / "Module.bsl"
            if src.fmt == "configurator"
            else form_dir / "Module.bsl"
        )
        return p if p.is_file() else None

    def form_xml_path(self, src: LiteSource, obj_dir: Path, form: str) -> Path | None:
        if src.fmt == "configurator":
            p = obj_dir / "Forms" / form / "Ext" / "Form.xml"
        else:
            p = obj_dir / "Forms" / form / "Form.form"
        return p if p.is_file() else None

    def available_modules(self, src: LiteSource, obj_dir: Path) -> list[str]:
        base = obj_dir / "Ext" if src.fmt == "configurator" else obj_dir
        out = [p.stem for p in sorted(base.glob("*.bsl"))] if base.is_dir() else []
        forms_dir = obj_dir / "Forms"
        if forms_dir.is_dir():
            for d in sorted(forms_dir.iterdir()):
                if d.is_dir() and self.form_module_path(src, obj_dir, d.name):
                    out.append(f"Form:{d.name}")
        return out

    def _require_ref(self, src: LiteSource, kind: str, name: str) -> ObjectRef | tuple[None, None, None]:
        self.listing(src)
        ref = self._by_kind_name[src.name].get((kind, name.lower()))
        return ref if ref else (None, None, None)

    # ------------------------------------------------------------------ files

    def bsl_files(self, src: LiteSource, kinds: set[str] | None = None,
                  fresh: bool = False) -> list[Path]:
        """All .bsl files of a source (kind folders only), TTL-cached when unfiltered."""
        if kinds is None:
            now = time.monotonic()
            hit = self._bsl.get(src.name)
            if hit and not fresh and now - hit[0] < 4 * _LIST_TTL:
                return hit[1]
        from ..parsing.dump import TYPE_FOLDERS

        wanted = set(TYPE_FOLDERS) if kinds is None else {
            folder for folder, kind in TYPE_FOLDERS.items() if kind in kinds
        }
        out: list[Path] = []
        for folder in sorted(wanted):
            kdir = src.files_root / folder
            if not kdir.is_dir():
                continue
            for walk_root, _dirs, names in os.walk(kdir):
                for n in names:
                    if n.endswith(".bsl"):
                        out.append(Path(walk_root) / n)
        if kinds is None:
            self._bsl[src.name] = (time.monotonic(), out)
        return out

    def safe_path(self, src: LiteSource, rel_path: str) -> tuple[Path | None, str]:
        """Resolve a path relative to the source's files_root, refusing escapes."""
        p = (src.files_root / rel_path).resolve()
        try:
            p.relative_to(src.files_root.resolve())
        except ValueError:
            return None, "Путь выходит за пределы источника."
        if not p.is_file():
            return None, f"Файл не найден: {rel_path}"
        return p, ""

    def refresh(self) -> None:
        """Drop all caches (next call re-reads the file system)."""
        self._listing.clear()
        self._by_kind_name.clear()
        self._objects.clear()
        self._bsl.clear()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")
