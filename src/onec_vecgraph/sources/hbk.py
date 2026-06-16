"""1C platform-help (syntax assistant) corpus adapter — reads .hbk help containers.

Public corpus → ingest into the shared tenant: `ingest <manifest> --tenant-id __shared__`.
source='platform_help'; per platform VERSION (manifest entry); version recorded so multiple builds
coexist in the shared tenant. Content is proprietary 1C platform documentation — keep the shared
tenant/repo private (internal use under the platform licence).

Manifest entry (`type: hbk`) accepts several path forms:
  - bin: "<.../1cv8/8.3.27.1989/bin>"   # auto-discovers sh*_ru.hbk; version parsed from the path
  - bins: ["<.../1cv8/*/bin>"]          # glob → one (version) per matched dir
  - files: ["/help/shcntx_ru.hbk", ...] # explicit file paths
  - domains: [shcntx, shlang, shquery]  # which sh* files to take from bin/bins (default: shcntx, shlang)
  - platform_version: "8.3.27.1989"     # explicit override (else parsed from the path)
  - limit: 200                          # cap pages per file (smoke/dev)
"""

from __future__ import annotations

import glob
import hashlib
import re
from pathlib import Path
from typing import Iterator

from lxml import html as lhtml

from .base import DocUnit, Source, sha1_text
from .hbk_container import iter_html_pages

_VER = re.compile(r"\d+\.\d+\.\d+\.\d+")
_NAME = re.compile(r"^(?P<ru>.+?)\s*\((?P<en>[^()]+)\)\s*$", re.S)
_DEFAULT_DOMAINS = ("shcntx", "shlang")
_HELP_KIND = {"shcntx": "context", "shlang": "language", "shquery": "query", "shclang": "client_language"}


def _version_from_path(p: str) -> str | None:
    for part in Path(p).parts:
        if _VER.fullmatch(part):
            return part
    m = _VER.search(p)
    return m.group(0) if m else None


def _help_kind(hbk_path: str) -> str:
    stem = Path(hbk_path).stem.lower()  # e.g. shcntx_ru
    for prefix, kind in _HELP_KIND.items():
        if stem.startswith(prefix):
            return kind
    return "other"


def _parse_page(html_bytes: bytes) -> tuple[str, str | None, str]:
    """-> (ru_name/title, en_name|None, plain_text). Name comes from <h1> (fallback <title>)."""
    doc = lhtml.fromstring(html_bytes)
    title = (doc.findtext(".//h1") or doc.findtext(".//title") or "").strip()
    m = _NAME.match(title)
    ru, en = (m.group("ru").strip(), m.group("en").strip()) if m else (title, None)
    text = re.sub(r"[ \t]*\n[ \t\n]*", "\n", doc.text_content()).strip()
    return ru, en, text


class HbkSource(Source):
    name = "hbk"
    source = "platform_help"
    owner_label = "Document"

    def __init__(self, entry: dict) -> None:
        self.entry = entry
        self.domains = tuple(entry.get("domains") or _DEFAULT_DOMAINS)
        self.limit = entry.get("limit")
        self.pv_override = entry.get("platform_version")
        self.help_kind_override = entry.get("help_kind")

    def _files(self) -> list[tuple[str, str, str]]:
        """Resolve the manifest entry to [(hbk_path, platform_version, help_kind)]."""
        out: list[tuple[str, str, str]] = []
        explicit = list(self.entry.get("files") or [])
        bins: list[str] = []
        if self.entry.get("bin"):
            bins.append(self.entry["bin"])
        for pattern in self.entry.get("bins") or []:
            bins.extend(sorted(glob.glob(pattern)))
        for d in bins:
            for dom in self.domains:
                explicit.extend(sorted(glob.glob(str(Path(d) / f"{dom}*_ru.hbk")))
                                or glob.glob(str(Path(d) / f"{dom}*.hbk")))
        seen: set[str] = set()
        for f in explicit:
            f = str(f)
            if f in seen or not Path(f).is_file():
                continue
            seen.add(f)
            pv = self.pv_override or _version_from_path(f) or "unknown"
            out.append((f, pv, self.help_kind_override or _help_kind(f)))
        return out

    def validate(self) -> list[tuple[str, str, str]]:
        """Resolve and CHECK the help path; raise a clear error instead of silently yielding nothing.
        Returns [(hbk_path, platform_version, help_kind)]."""
        if not (self.entry.get("bin") or self.entry.get("bins") or self.entry.get("files")):
            raise ValueError(
                "hbk source: no help path specified — set 'bin' (platform bin dir), 'bins' (glob) "
                "or 'files' (explicit .hbk paths) in the manifest entry / CLI options."
            )
        files = self._files()
        if not files:
            tried = self.entry.get("files") or self.entry.get("bins") or [self.entry.get("bin")]
            raise FileNotFoundError(
                f"hbk source: no .hbk help files found (looked at: {tried}; domains={list(self.domains)}). "
                "Check the path and that sh*_ru.hbk exist there."
            )
        return files

    def units(self) -> Iterator[DocUnit]:
        for hbk_path, pv, help_kind in self.validate():
            n = 0
            for zip_path, html in iter_html_pages(hbk_path):
                try:
                    ru, en, text = _parse_page(html)
                except Exception:  # noqa: BLE001 - skip malformed page, keep going
                    continue
                if not text:
                    continue
                full = ru or zip_path
                ext_id = f"{pv}|{full}"
                section = [p for p in zip_path.split("/")[:-1] if p]
                yield DocUnit(
                    external_id=ext_id,
                    title=ru or zip_path,
                    text=text,
                    version_hash=sha1_text(pv, zip_path, hashlib.sha1(html).hexdigest()),
                    section_path=section,
                    source_url=zip_path,
                    extra={
                        "doc_topic": "platform",  # platform-level help, not configuration-specific
                        "platform_version": pv,
                        "help_kind": help_kind,
                        "name_norm": (full.split(".")[-1] if full else "").lower(),
                        "full_name_norm": full.lower(),
                        "en_name": en,
                    },
                )
                n += 1
                if self.limit and n >= self.limit:
                    break
