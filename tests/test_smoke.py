"""End-to-end smoke test on a tiny dataset: every workload, every engine variant."""

from __future__ import annotations

import json
import math

import pytest
from rich.console import Console

from duckdb_vs_sqlite.cli import main
from duckdb_vs_sqlite.config import ENGINES, SizeSpec, make_config, parse_sizes
from duckdb_vs_sqlite.report import PLACEHOLDER, build_dashboard
from duckdb_vs_sqlite.runner import Runner, save_results
from duckdb_vs_sqlite.stats import percentile, summarize
from duckdb_vs_sqlite.workloads import WORKLOADS


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    work = tmp_path_factory.mktemp("work")
    cfg = make_config(
        "quick",
        sizes=[SizeSpec("small", "Small", 300), SizeSpec("large", "Large", 1500)],
        engines=list(ENGINES.values()),
        repeats=2,
        oltp_read_ops=40,
        oltp_write_ops=25,
        batch_rows=50,
        budget_s=2.0,
    )
    runner = Runner(cfg, work / "scratch", console=Console(quiet=True))
    out = runner.run()
    assert not (work / "scratch").exists(), "scratch databases should be cleaned up"
    return out


def test_every_cell_was_measured(results):
    failed = [r for r in results["results"] if r.get("error")]
    assert not failed, failed
    seen = {(r["engine"], r["size"], r["width"], r["workload"]) for r in results["results"]}
    expected = {
        (e["key"], s["key"], w["key"], wl.key)
        for e in results["engines"] for s in results["sizes"] for w in results["widths"] for wl in WORKLOADS
    }
    assert seen == expected
    for r in results["results"]:
        assert r["n"] >= 1 and r["median"] > 0 and r["min"] <= r["median"] <= r["max"]
        assert ("samples" in r) != ("quantiles" in r)


def test_engines_return_the_same_answers(results):
    counts: dict[tuple, set] = {}
    for r in results["results"]:
        if "result_rows" in r:
            counts.setdefault((r["size"], r["width"], r["workload"]), set()).add(r["result_rows"])
    assert counts, "read workloads should record how many rows they returned"
    assert {k: v for k, v in counts.items() if len(v) != 1} == {}


def test_storage_is_recorded(results):
    assert len(results["storage"]) == len(results["engines"]) * len(results["sizes"]) * len(results["widths"])
    assert all(s["bytes"] > 0 for s in results["storage"])


def test_results_and_dashboard_files(results, tmp_path):
    path = save_results(results, tmp_path)
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == results["run_id"]
    assert (tmp_path / "latest.json").exists()
    dashboard = build_dashboard([results, results], tmp_path / "dashboard.html")
    html = dashboard.read_text(encoding="utf-8")
    assert PLACEHOLDER not in html
    assert results["run_id"] in html
    assert "</script>" not in json.dumps(results)  # sanity: nothing that could break out of the data block


def test_cli_report_and_list(results, tmp_path, capsys):
    path = save_results(results, tmp_path)
    assert main(["report", str(path), "-o", str(tmp_path / "d.html"), "--no-open"]) == 0
    assert (tmp_path / "d.html").stat().st_size > 10_000
    assert main(["list"]) == 0
    assert "point_select" in capsys.readouterr().out


def test_parse_sizes():
    sizes = parse_sizes("big=2_000_000, small=5000")
    assert [(s.key, s.rows) for s in sizes] == [("small", 5000), ("big", 2_000_000)]
    assert parse_sizes("1e4")[0].label == "10K"
    with pytest.raises(ValueError):
        parse_sizes("small=10")


def test_stats():
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    s = summarize([0.3, 0.1, 0.2])
    assert s["n"] == 3 and math.isclose(s["median"], 0.2) and s["samples"] == [0.3, 0.1, 0.2]
    big = summarize([i / 1000 for i in range(1, 501)])
    assert len(big["quantiles"]) == 101 and "samples" not in big
