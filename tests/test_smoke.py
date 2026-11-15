"""End-to-end smoke test on a tiny dataset: every workload, every engine variant."""

from __future__ import annotations

import json
import math

import pytest
from rich.console import Console

from duckdb_vs_sqlite.cli import main
from duckdb_vs_sqlite.config import ENGINES, SizeSpec, human_rows, make_config, parse_sizes
from duckdb_vs_sqlite.engines import DuckDBEngine, SqliteEngine
from duckdb_vs_sqlite.report import PLACEHOLDER, build_dashboard
from duckdb_vs_sqlite.runner import Runner, save_results
from duckdb_vs_sqlite.stats import percentile, summarize
from duckdb_vs_sqlite.workloads import WORKLOADS, Ctx, _fetch_rows_range, _run_cold_start


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
    for bad in ("abc", "small=nan", "inf", "1e400", "a=1=2", "1000.9", "_1000", "1__000"):
        with pytest.raises(ValueError, match="database size must be a whole number of rows"):
            parse_sizes(bad)


def test_human_rows_rounds_before_choosing_a_format():
    assert [human_rows(n) for n in (999, 1_000, 1_500, 999_999, 2_000_000, 999_999_999)] == [
        "999", "1K", "1.5K", "1000K", "2M", "1000M",
    ]


def test_stats():
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    s = summarize([0.3, 0.1, 0.2])
    assert s["n"] == 3 and math.isclose(s["median"], 0.2) and s["samples"] == [0.3, 0.1, 0.2]
    big = summarize([i / 1000 for i in range(1, 501)])
    assert len(big["quantiles"]) == 101 and "samples" not in big


def test_workdir_with_other_files_is_left_alone(tmp_path):
    keep = tmp_path / "precious.txt"
    keep.write_text("not a scratch file", encoding="utf-8")
    cfg = _tiny_config(repeats=1, budget_s=1.0)
    Runner(cfg, tmp_path, console=Console(quiet=True), only="point_select").run()
    assert [p.name for p in tmp_path.iterdir()] == ["precious.txt"]


def test_cold_start_keys_and_export_range():
    def ctx(rows, **overrides):
        return Ctx(engine=None, cfg=make_config("quick", **overrides), staging=None, db_path=None,
                   rows=rows, width="narrow", customers=100)

    for rows in (300, 5000, 6000, 7500, 1_000_000):
        lo, hi = _fetch_rows_range(ctx(rows))
        assert 1 <= lo and hi <= rows and hi - lo + 1 == min(5000, rows)
    small = ctx(2000, oltp_write_ops=5, batch_rows=10)
    assert small.cfg.extra_rows >= small.cfg.oltp_write_ops + small.write_ops + 5 * small.cfg.batch_rows

    # Cold start must look up different rows, not one key over and over.
    seen: list[int] = []

    class Con:
        def execute(self, _sql, params):
            seen.append(params[0])
            return self

        def fetchall(self):
            return []

        def close(self):
            pass

    class FakeEngine:
        def connect(self, _path):
            return Con()

    cold = ctx(1_000_000)
    cold.engine = FakeEngine()
    _run_cold_start(cold)
    assert len(set(seen)) >= 5


def test_dashboard_data_cannot_escape_its_script_block(tmp_path):
    run = {"note": "<!--<script></script>"}
    html = build_dashboard([run], tmp_path / "d.html").read_text(encoding="utf-8")
    start = html.index("window.BENCH_DATA = ") + len("window.BENCH_DATA = ")
    payload = html[start : html.index(";</script>", start)]
    assert "<" not in payload and json.loads(payload) == {"runs": [run]}


def _run_cli(tmp_path, *argv):
    # Quick, headless and confined to tmp_path: if a check regresses, the benchmark that
    # starts must be small and must not touch the repository or open a browser.
    return main(["run", "--preset", "quick", "--no-open", "--out", str(tmp_path / "out"),
                 "--workdir", str(tmp_path / "work"), *argv])


@pytest.mark.parametrize("argv", [
    ["--repeats", "0"], ["--budget", "-1"], ["--budget", "inf"], ["--oltp-write-ops", "-5"],
    ["--only", "("], ["--rows", "inf"], ["--rows", ""], ["--engines", ","], ["--widths", ","],
    ["--engines", "nope"], ["--engines", "[/x]"], ["--rows", "a=[/b]"], ["--rows", "abc"],
    ["--only", "[/b]q"],
])
def test_cli_reports_invalid_options(argv, tmp_path, capsys):
    assert _run_cli(tmp_path, *argv) == 2
    assert "invalid option" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()


def test_cli_names_the_unknown_value(tmp_path, capsys):
    assert _run_cli(tmp_path, "--widths", "narow") == 2
    out = capsys.readouterr().out
    assert "row width" in out and "narow" in out and "narrow" in out


def test_config_is_validated_without_the_cli():
    small, twin = SizeSpec("s", "S", 50), SizeSpec("a", "A", 500)
    for bad in ({"repeats": 0}, {"repeats": 2.5}, {"budget_s": float("nan")}, {"budget_s": "4"},
                {"engines": []}, {"widths": []}, {"sizes": []}, {"sizes": [small]}, {"sizes": [twin, twin]},
                {"sqlite_synchronous": "FULL; --"}, {"sqlite_synchronous": "full"}, {"batch_rows": 0}):
        with pytest.raises(ValueError):
            make_config("quick", **bad)


def test_duckdb_spill_directory_is_removed(tmp_path):
    db = tmp_path / "bench.duckdb"
    for leftover in (db, tmp_path / "bench.duckdb.wal"):
        leftover.write_bytes(b"x")
    (tmp_path / "bench.duckdb.tmp").mkdir()
    (tmp_path / "bench.duckdb.tmp" / "duckdb_temp_storage-0.tmp").write_bytes(b"x")
    DuckDBEngine.remove_files(db)
    DuckDBEngine.remove_files(db)  # nothing left: still must not raise
    assert list(tmp_path.iterdir()) == []


def test_labels_are_not_parsed_as_markup(tmp_path):
    # A size name that looks like a Rich closing tag must print, not raise.
    assert _run_cli(tmp_path, "--rows", "[/a]=300", "--widths", "narrow", "--engines", "sqlite",
                    "--only", "point_select") == 0
    assert not (tmp_path / "work").exists()


@pytest.mark.parametrize("content", ["{nope", "[1, 2]", '{"schema_version": 1}', None])
def test_cli_report_rejects_files_that_are_not_results(content, tmp_path, capsys):
    path = tmp_path / "in.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert main(["report", str(path), "-o", str(tmp_path / "d.html"), "--no-open"]) == 2
    assert "invalid results file" in capsys.readouterr().out
    assert not (tmp_path / "d.html").exists()


def test_cli_report_rejects_other_schema_versions(results, tmp_path, capsys):
    path = tmp_path / "future.json"
    path.write_text(json.dumps({**results, "schema_version": 99}), encoding="utf-8")
    assert main(["report", str(path), "-o", str(tmp_path / "d.html"), "--no-open"]) == 2
    assert "schema" in capsys.readouterr().out


def test_cli_report_names_the_schema_even_when_keys_differ(tmp_path, capsys):
    path = tmp_path / "future.json"
    path.write_text('{"schema_version": 2, "measurements": []}', encoding="utf-8")
    assert main(["report", str(path), "-o", str(tmp_path / "d.html"), "--no-open"]) == 2
    assert "schema 2" in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["--out", "--workdir"])
def test_cli_checks_directories_before_running(flag, tmp_path, capsys):
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    assert _run_cli(tmp_path, flag, str(blocker)) == 2  # the later flag wins over _run_cli's default
    assert f"cannot use {flag}" in capsys.readouterr().out
    assert not (tmp_path / "work").exists()


def test_sizes_are_ordered_smallest_first():
    cfg = make_config("quick", sizes=[SizeSpec("large", "Large", 5000), SizeSpec("small", "Small", 300)])
    assert [s.key for s in cfg.sizes] == ["small", "large"]


@pytest.mark.parametrize("argv, flag", [
    (["--budget", "0"], "--budget"), (["--repeats", "0"], "--repeats"),
    (["--oltp-read-ops", "0"], "--oltp-read-ops"), (["--oltp-write-ops", "-1"], "--oltp-write-ops"),
])
def test_cli_errors_name_the_flag(argv, flag, tmp_path, capsys):
    assert _run_cli(tmp_path, *argv) == 2
    out = capsys.readouterr().out
    assert f"invalid option: {flag} must be" in out and "budget_s" not in out and "_ops" not in out


def test_cli_report_with_unwritable_output(results, tmp_path, capsys):
    path = save_results(results, tmp_path)
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    assert main(["report", str(path), "-o", str(blocker / "d.html"), "--no-open"]) == 2
    assert "cannot write the dashboard" in capsys.readouterr().out


def _tiny_config(**overrides):
    """One 300-row database on SQLite, both row widths."""
    return make_config("quick", **{
        "sizes": [SizeSpec("small", "Small", 300)], "engines": [ENGINES["sqlite"]],
        "repeats": 3, "oltp_read_ops": 20, "oltp_write_ops": 20, "batch_rows": 20, "budget_s": 2.0, **overrides,
    })


def test_cli_only_must_match_a_workload(tmp_path, capsys):
    assert _run_cli(tmp_path, "--only", "pont_select") == 2
    out = capsys.readouterr().out
    assert "invalid option: --only pont_select matches no workload" in out and "dvs list" in out
    assert not (tmp_path / "out").exists()


def test_bulk_delete_never_times_an_empty_delete(tmp_path):
    # 20 runs delete the 20 buckets; a 21st would find nothing and look instant.
    # The budget is generous so that only the bucket count can end the loop, even on a slow disk.
    cfg = _tiny_config(repeats=23, budget_s=60.0)
    runner = Runner(cfg, tmp_path / "w", console=Console(quiet=True), only="^bulk_delete$")
    records = runner.run()["results"]
    assert [r["n"] for r in records] == [20, 20]  # one per row width


def test_bulk_statements_count_the_rows_the_table_holds_by_then(tmp_path):
    # batch_insert adds 5 batches of 20 rows to the 300 loaded: each bulk statement covers 1/20 of 400.
    cfg = _tiny_config(budget_s=60.0)
    runner = Runner(cfg, tmp_path / "w", console=Console(quiet=True), only="^(batch_insert|bulk_update|bulk_delete)$")
    records = runner.run()["results"]
    assert {r["rows_per_op"] for r in records if r["workload"].startswith("bulk_")} == {20.0}


def test_unselected_load_and_index_run_once(tmp_path, monkeypatch):
    loads: list[int] = []
    original = SqliteEngine.bulk_load_orders

    def counting(self, con, staging):
        loads.append(1)
        return original(self, con, staging)

    monkeypatch.setattr(SqliteEngine, "bulk_load_orders", counting)
    cfg = _tiny_config(budget_s=60.0)
    out = Runner(cfg, tmp_path / "a", console=Console(quiet=True), only="point_select").run()
    assert len(loads) == len(cfg.widths) and {r["workload"] for r in out["results"]} == {"point_select"}

    loads.clear()
    out = Runner(cfg, tmp_path / "b", console=Console(quiet=True), only="bulk_load|point_select").run()
    assert len(loads) == len(cfg.widths) * cfg.repeats  # selected: every repetition is a sample
    assert {r["n"] for r in out["results"] if r["workload"] == "bulk_load"} == {cfg.repeats}
