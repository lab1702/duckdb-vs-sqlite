"""Builds the self-contained HTML dashboard from one or more result sets."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

PLACEHOLDER = "/*__BENCH_DATA__*/null"


def build_dashboard(runs: list[dict], output: Path) -> Path:
    """Embed ``runs`` into the dashboard template and write a single HTML file.

    The file has no external dependencies: it can be opened from disk, e-mailed or
    hosted as-is.
    """
    template = resources.files("duckdb_vs_sqlite").joinpath("dashboard.html").read_text(encoding="utf-8")
    if PLACEHOLDER not in template:
        raise RuntimeError("dashboard template is missing its data placeholder")
    payload = json.dumps({"runs": runs}, ensure_ascii=False, separators=(",", ":"))
    # Keep the JSON inert inside a <script> block.
    payload = payload.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(template.replace(PLACEHOLDER, payload), encoding="utf-8")
    return output
