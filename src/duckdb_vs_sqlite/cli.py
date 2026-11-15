"""Command line interface: ``dvs run``, ``dvs report``, ``dvs list``."""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
import webbrowser
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .config import ENGINES, PRESETS, SQLITE_SYNCHRONOUS, WIDTHS, FieldError, make_config, parse_sizes

DEFAULT_OUT = Path("results")
DEFAULT_WORKDIR = Path(".bench-work")


def _split(text: str) -> list[str]:
    """Comma-separated list without blanks or repeats, order preserved."""
    return list(dict.fromkeys(part.strip() for part in text.split(",") if part.strip()))


def _lookup(kind: str, table: dict, keys: list[str]) -> list:
    unknown = [k for k in keys if k not in table]
    if unknown:
        raise ValueError(f"unknown {kind} {', '.join(unknown)} (available: {', '.join(table)})")
    return [table[k] for k in keys]


def _check_dirs(out: Path, workdir: Path) -> None:
    """Fail before the benchmark, not during or after it, if a directory cannot be used."""
    for flag, directory in (("--out", out), ("--workdir", workdir)):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            # Write for real: os.access() ignores ACLs on Windows and can be wrong on network drives.
            with tempfile.TemporaryFile(dir=directory):
                pass
        except OSError as exc:
            raise ValueError(f"cannot use {flag} {directory}: {exc.strerror or exc}") from None


# BenchConfig names fields; on the command line the user typed a flag.
_FLAG_OF_FIELD = {
    "repeats": "--repeats",
    "oltp_read_ops": "--oltp-read-ops",
    "oltp_write_ops": "--oltp-write-ops",
    "budget_s": "--budget",
    "sqlite_synchronous": "--sqlite-synchronous",
}


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
    run.add_argument("--sqlite-synchronous", choices=SQLITE_SYNCHRONOUS,
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

    # Each flag's dest is its name: "--oltp-read-ops" -> args.oltp_read_ops.
    overrides: dict = {
        field: getattr(args, flag.lstrip("-").replace("-", "_")) for field, flag in _FLAG_OF_FIELD.items()
    }
    try:
        if args.rows is not None:
            overrides["sizes"] = parse_sizes(args.rows)
        if args.widths is not None:
            overrides["widths"] = _lookup("row width", WIDTHS, _split(args.widths))
        engine_keys = _split("sqlite,duckdb" if args.engines is None else args.engines)
        if engine_keys and args.duckdb_single_thread and "duckdb-1t" not in engine_keys:
            engine_keys.append("duckdb-1t")
        overrides["engines"] = _lookup("engine", ENGINES, engine_keys)
        if args.only:
            re.compile(args.only)
        cfg = make_config(args.preset, **overrides)  # validates the combined configuration
        _check_dirs(args.out, args.workdir)
    except (ValueError, re.error) as exc:
        message = f"{_FLAG_OF_FIELD.get(exc.field, exc.field)} {exc.problem}" if isinstance(exc, FieldError) else str(exc)
        console.print(f"[red]invalid option:[/red] {escape(message)}")
        return 2

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
    from .report import build_dashboard, load_results

    paths = args.results or [DEFAULT_OUT / "latest.json"]
    try:
        runs = [load_results(path) for path in paths]
    except ValueError as exc:
        console.print(f"[red]invalid results file:[/red] {escape(str(exc))}")
        return 2
    output = args.output or paths[0].parent / "dashboard.html"
    try:
        dashboard = build_dashboard(runs, output)
    except OSError as exc:
        console.print(f"[red]cannot write the dashboard:[/red] {escape(f'{output}: {exc.strerror or exc}')}")
        return 2
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
