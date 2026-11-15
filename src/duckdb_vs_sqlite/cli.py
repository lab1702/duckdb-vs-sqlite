"""Command line interface: ``dvs run``, ``dvs report``, ``dvs list``."""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .config import ENGINES, PRESETS, WIDTHS, make_config, parse_sizes

DEFAULT_OUT = Path("results")
DEFAULT_WORKDIR = Path(".bench-work")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dvs",
        description="Benchmark SQLite against DuckDB and explore the results in an interactive dashboard.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the benchmark and build the dashboard")
    run.add_argument("--preset", choices=sorted(PRESETS), default="standard",
                     help="quick: seconds, for a sanity check; standard (default): 10K and 1M rows, minutes; "
                          "full: adds a third size and goes to 5M rows")
    run.add_argument("--rows", metavar="SPEC",
                     help="database sizes, e.g. 'small=10000,large=2000000' (overrides the preset)")
    run.add_argument("--widths", metavar="LIST", help="row widths to test: narrow,wide (default: both)")
    run.add_argument("--engines", metavar="LIST",
                     help=f"engine variants to test (default: sqlite,duckdb). Available: {', '.join(ENGINES)}")
    run.add_argument("--duckdb-single-thread", action="store_true",
                     help="also run DuckDB limited to one thread, to separate parallelism from engine design")
    run.add_argument("--repeats", type=int, help="timed repetitions of each analytical query")
    run.add_argument("--oltp-read-ops", type=int, help="lookups per OLTP read workload")
    run.add_argument("--oltp-write-ops", type=int, help="commits per OLTP write workload")
    run.add_argument("--budget", type=float, metavar="SECONDS",
                     help="soft time limit per workload, per engine, per database")
    run.add_argument("--sqlite-synchronous", choices=["FULL", "NORMAL", "OFF"],
                     help="SQLite durability (default FULL: fsync every commit, like DuckDB)")
    run.add_argument("--only", metavar="REGEX", help="only record workloads whose key matches (see 'dvs list')")
    run.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory (default: ./results)")
    run.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR,
                     help="scratch directory for database files; put it on the disk you want to measure")
    run.add_argument("--keep-data", action="store_true", help="keep the scratch database files afterwards")
    run.add_argument("--no-open", action="store_true", help="do not open the dashboard in a browser")

    report = sub.add_parser("report", help="rebuild the dashboard from saved results")
    report.add_argument("results", nargs="*", type=Path,
                        help="one or more results-*.json files (default: results/latest.json). "
                             "Several files - e.g. from different machines - become a run selector.")
    report.add_argument("-o", "--output", type=Path, help="dashboard path (default: next to the first results file)")
    report.add_argument("--no-open", action="store_true", help="do not open the dashboard in a browser")

    sub.add_parser("list", help="list the workloads")
    return parser


def _print_path(console: Console, label: str, path: Path) -> None:
    # Never wrap or restyle a path: it has to survive copy and paste.
    console.print(f"{label} {path}", soft_wrap=True, highlight=False, markup=False)


def _open(path: Path, no_open: bool) -> None:
    if not no_open:
        try:
            webbrowser.open(path.resolve().as_uri())
        except Exception:
            pass


def _cmd_run(args: argparse.Namespace, console: Console) -> int:
    from .report import build_dashboard
    from .runner import Runner, save_results

    overrides: dict = {
        "repeats": args.repeats,
        "oltp_read_ops": args.oltp_read_ops,
        "oltp_write_ops": args.oltp_write_ops,
        "budget_s": args.budget,
        "sqlite_synchronous": args.sqlite_synchronous,
    }
    try:
        if args.rows:
            overrides["sizes"] = parse_sizes(args.rows)
        if args.widths:
            overrides["widths"] = [WIDTHS[w.strip()] for w in args.widths.split(",") if w.strip()]
        engine_keys = [e.strip() for e in (args.engines or "sqlite,duckdb").split(",") if e.strip()]
        if args.duckdb_single_thread and "duckdb-1t" not in engine_keys:
            engine_keys.append("duckdb-1t")
        overrides["engines"] = [ENGINES[k] for k in engine_keys]
    except (KeyError, ValueError) as exc:
        console.print(f"[red]invalid option:[/red] {exc}")
        return 2
    cfg = make_config(args.preset, **overrides)

    runner = Runner(cfg, args.workdir, console=console, only=args.only, keep_data=args.keep_data)
    results = runner.run()
    results_path = save_results(results, args.out)
    dashboard = build_dashboard([results], args.out / "dashboard.html")
    console.print(f"finished in {results['duration_s']:.0f} s")
    _print_path(console, "results:  ", results_path)
    _print_path(console, "dashboard:", dashboard)
    _open(dashboard, args.no_open)
    return 0


def _cmd_report(args: argparse.Namespace, console: Console) -> int:
    from .report import build_dashboard

    paths = args.results or [DEFAULT_OUT / "latest.json"]
    runs = []
    for path in paths:
        if not path.exists():
            console.print(f"[red]no such results file:[/red] {path}")
            return 2
        runs.append(json.loads(path.read_text(encoding="utf-8")))
    output = args.output or paths[0].parent / "dashboard.html"
    dashboard = build_dashboard(runs, output)
    _print_path(console, "dashboard:", dashboard)
    _open(dashboard, args.no_open)
    return 0


def _cmd_list(console: Console) -> int:
    from .workloads import WORKLOADS

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    for column in ("key", "class", "workload", "one sample is"):
        table.add_column(column)
    for w in WORKLOADS:
        table.add_row(w.key, f"{w.category} {w.kind}", w.label, f"one {w.unit}")
    console.print(table)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    console = Console()
    if args.command == "run":
        return _cmd_run(args, console)
    if args.command == "report":
        return _cmd_report(args, console)
    return _cmd_list(console)


if __name__ == "__main__":
    sys.exit(main())
