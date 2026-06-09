"""Load a per-tenant source manifest (YAML primary; JSON also accepted).

Shape:
    tenant: projectA            # optional (CLI --tenant-id overrides)
    sources:
      - {type: config_dump, path: /dumps/ERP_UH, code: true}
      - {type: git_artifacts, repo: git@…:projectA/docs.git, branch: main, globs: ["**/*.md"]}
      - {type: its, repo: git@…:projectA/its.git}        # or {path: /its/projectA}
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def load_manifest(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        import yaml  # lazy: only needed for YAML manifests (extra 'ingest')

        data = yaml.safe_load(raw)
    elif p.suffix.lower() == ".json":
        import json

        data = json.loads(raw)
    else:
        raise ValueError(f"Unsupported manifest format: {p.suffix} (use .yaml/.yml/.json)")
    if not isinstance(data, dict) or "sources" not in data:
        raise ValueError("manifest must be a mapping with a 'sources' list")
    return data
