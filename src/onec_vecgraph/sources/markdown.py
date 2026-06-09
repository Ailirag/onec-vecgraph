"""Split a Markdown/AsciiDoc-ish document into logical sections by headings.

Each section keeps its heading title and the breadcrumb path of ancestor headings, so a chunk is
self-contained. Preamble before the first heading becomes a section with an empty title.
"""

from __future__ import annotations

import re

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def split_markdown_sections(text: str) -> list[dict]:
    """-> [{title, path: [ancestor titles], body}]. Sections with empty body are still returned;
    callers filter."""
    sections: list[dict] = []
    stack: list[tuple[int, str]] = []  # (level, title) ancestors
    cur_title = ""
    cur_path: list[str] = []
    cur_body: list[str] = []

    def flush() -> None:
        sections.append({"title": cur_title, "path": list(cur_path), "body": "\n".join(cur_body).strip()})

    for line in text.split("\n"):
        m = _HEADING.match(line)
        if not m:
            cur_body.append(line)
            continue
        # new heading -> close previous section
        flush()
        level = len(m.group(1))
        title = m.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        cur_path = [t for _, t in stack]
        stack.append((level, title))
        cur_title = title
        cur_body = []
    flush()
    return sections
