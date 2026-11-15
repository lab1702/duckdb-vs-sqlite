"""Builds the self-contained HTML dashboard from one or more result sets."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from .config import SCHEMA_VERSION

PLACEHOLDER = "/*__BENCH_DATA__*/null"
RUN_KEYS = ("system", "config", "sizes", "widths", "engines", "workloads", "results")


def load_results(path: Path) -> dict:
    """Read one results file written by ``save_results``. ValueError if it is not one."""
    try:
        run = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc.strerror or exc}") from None
    except ValueError as exc:  # malformed JSON or not UTF-8
        raise ValueError(f"{path} is not a results file: {exc}") from None
    if not isinstance(run, dict) or "schema_version" not in run:
        raise ValueError(f"{path} is not a results file written by 'dvs run'")
    # Before looking at the contents: another schema may well have other keys.
    if run["schema_version"] != SCHEMA_VERSION:
        raise ValueError(
            f"{path} has results schema {run['schema_version']!r}; this version reads schema {SCHEMA_VERSION}"
        )
    if any(key not in run for key in RUN_KEYS):
        raise ValueError(f"{path} is not a complete results file")
    return run


def build_dashboard(runs: list[dict], output: Path) -> Path:
    """Embed ``runs`` into the dashboard template and write a single HTML file.

    The file has no external dependencies: it can be opened from disk, e-mailed or
    hosted as-is.
    """
    template = resources.files("duckdb_vs_sqlite").joinpath("dashboard.html").read_text(encoding="utf-8")
    if PLACEHOLDER not in template:
        raise RuntimeError("dashboard template is missing its data placeholder")
    payload = json.dumps({"runs": runs}, ensure_ascii=False, separators=(",", ":"))
    # Keep the JSON inert inside a <script> block: with no literal "<" it can neither close
    # the element ("</script>") nor open an HTML comment ("<!--<script" swallows the real end tag).
    payload = payload.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(template.replace(PLACEHOLDER, payload), encoding="utf-8")
    return output
