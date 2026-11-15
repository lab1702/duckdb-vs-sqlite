"""Runs every workload on every engine for every database shape and collects results."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import sysinfo
from .config import SCHEMA_VERSION, BenchConfig, SizeSpec, WidthSpec, human_rows
from .datagen import Staging, n_customers, order_columns
from .engines import DuckDBEngine, Engine, make_engine
from .stats import summarize
from .workloads import BY_KEY, EXECUTION_ORDER, WORKLOADS, Ctx

def format_seconds(value: float | None) -> str:
    if value is None:
        return "-"
    if value < 1e-6:
        return f"{value * 1e9:.0f} ns"
    if value < 1e-3:
        return f"{value * 1e6:.1f} us"
    if value < 1:
        return f"{value * 1e3:.2f} ms"
    return f"{value:.2f} s"


class Runner:
    def __init__(self, cfg: BenchConfig, workdir: Path, console: Console | None = None,
                 only: str | None = None, keep_data: bool = False):
        self.cfg = cfg
        self._setup_cfg = replace(cfg, repeats=1)  # for load/index runs nobody records
        self.workdir = workdir
        self.console = console or Console()
        self.only = re.compile(only) if only else None
        self.keep_data = keep_data
        self.results: list[dict] = []
        self.storage: list[dict] = []
        self.failures = 0  # workloads that raised, recorded or not

    # -- public -------------------------------------------------------------
    def run(self) -> dict:
        started = datetime.now(timezone.utc)
        t_start = perf_counter()
        engines = [make_engine(spec, self.cfg) for spec in self.cfg.engines]
        system = sysinfo.collect()
        # Labels and system strings are data, not markup.
        self.console.print(
            f"[bold]DuckDB {system['duckdb']} vs SQLite {system['sqlite']}[/bold]  "
            + escape(f"({system['python']}, {system['os']}, {system['cores_logical']} logical cores, "
                     f"{system['ram_gb']} GB RAM)")
        )
        self.console.print(
            f"preset [bold]{escape(self.cfg.preset)}[/bold]: "
            + escape(
                ", ".join(f"{s.label.lower()} = {s.rows:,} rows" for s in self.cfg.sizes)
                + f"; widths: {', '.join(w.key for w in self.cfg.widths)}"
                + f"; engines: {', '.join(e.spec.label for e in engines)}"
            )
            + "\n"
        )
        try:
            self.workdir.mkdir(parents=True, exist_ok=True)
            for size in self.cfg.sizes:
                for width in self.cfg.widths:
                    self._run_shape(size, width, engines)
        finally:
            if not self.keep_data:
                # Every scratch file is deleted by whoever created it. Only ever remove the
                # directory itself, and only if it is empty: --workdir may point at a
                # directory that holds the user's own files.
                try:
                    self.workdir.rmdir()
                except OSError:
                    pass

        finished = datetime.now(timezone.utc)
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": started.strftime("%Y%m%dT%H%M%SZ"),
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"),
            "duration_s": round(perf_counter() - t_start, 1),
            "system": system,
            "config": self.cfg.to_dict(),
            "sizes": [{"key": s.key, "label": s.label, "rows": s.rows} for s in self.cfg.sizes],
            "widths": [
                {"key": w.key, "label": w.label, "description": w.description,
                 "columns": len(order_columns(w.key))}
                for w in self.cfg.widths
            ],
            "engines": [
                {"key": e.spec.key, "label": e.spec.label, "short": e.spec.short, "kind": e.spec.kind,
                 "settings": e.settings()}
                for e in engines
            ],
            "workloads": [w.meta() for w in WORKLOADS],
            "results": self.results,
            "storage": self.storage,
        }

    # -- internals ----------------------------------------------------------
    def _selected(self, key: str) -> bool:
        return self.only is None or bool(self.only.search(key))

    def _run_shape(self, size: SizeSpec, width: WidthSpec, engines: list[Engine]) -> None:
        title = f"{size.label} database ({human_rows(size.rows)} rows) x {width.label.lower()}"
        self.console.rule(f"[bold]{escape(title)}")
        # Named like the benchmark databases: --workdir may hold a "staging.duckdb" of the user's.
        staging_path = self.workdir / "bench-staging.duckdb"
        DuckDBEngine.remove_files(staging_path)  # leftovers of an interrupted run
        staging = Staging(staging_path, size.rows, width.key, self.cfg.extra_rows, self.cfg.seed)
        shape_results: dict[str, dict[str, dict]] = {}
        try:
            with self.console.status(f"generating {size.rows + self.cfg.extra_rows:,} rows ..."):
                t0 = perf_counter()
                staging.build()
                pool = staging.extra_tuples()
            self.console.print(f"[dim]generated dataset in {perf_counter() - t0:.1f} s[/dim]")
            for engine in engines:
                shape_results[engine.spec.key] = self._run_engine(engine, size, width, staging, pool)
        finally:
            staging.close()
            DuckDBEngine.remove_files(staging_path)
        self._print_shape_table(engines, shape_results)

    def _run_engine(self, engine: Engine, size: SizeSpec, width: WidthSpec,
                    staging: Staging, pool: list[tuple]) -> dict[str, dict]:
        ctx = Ctx(
            engine=engine, cfg=self.cfg, staging=staging,
            db_path=engine.db_path(self.workdir, size.rows, width.key),
            rows=size.rows, width=width.key, customers=n_customers(size.rows), pool=pool,
        )
        engine.remove_files(ctx.db_path)
        out: dict[str, dict] = {}
        label = engine.spec.label
        try:
            with self.console.status("") as status:
                for key in EXECUTION_ORDER:
                    workload = BY_KEY[key]
                    status.update(f"[bold]{label}[/bold]: {workload.label}")
                    if workload.phase == "cold":
                        # A second connection must not find an already-open instance.
                        engine.finalize_load(ctx.con)
                        ctx.con.close()
                        ctx.con = None
                        self.storage.append(self._storage_record(engine, size, width, ctx.db_path))
                    # Load and index always run - later workloads depend on them - but are
                    # only recorded when selected.
                    if workload.phase not in ("load", "index") and not self._selected(key):
                        if workload.phase == "cold":
                            ctx.con = engine.connect(ctx.db_path)
                        continue
                    # Unselected, they run once: repeating them would only time discarded samples.
                    ctx.cfg = self.cfg if self._selected(key) else self._setup_cfg
                    record = {"engine": engine.spec.key, "size": size.key, "width": width.key, "workload": key}
                    failed = False
                    try:
                        outcome = workload.run(ctx)
                        record.update(summarize(outcome.samples))
                        if outcome.rows_per_op:
                            record["rows_per_op"] = round(outcome.rows_per_op, 1)
                            if record.get("mean"):
                                record["rows_per_s"] = round(outcome.rows_per_op / record["mean"], 1)
                        if outcome.result_rows is not None:
                            record["result_rows"] = outcome.result_rows
                    except Exception as exc:  # keep going: one failure must not sink the run
                        failed = True
                        self.failures += 1
                        record.update({"n": 0, "error": f"{type(exc).__name__}: {exc}"[:500]})
                        self.console.print(f"[yellow]{escape(label)} / {key} failed: {escape(record['error'])}[/yellow]")
                        self._recover(ctx)
                    if self._selected(key):
                        self.results.append(record)
                        out[key] = record
                    if failed and workload.phase == "load":
                        # Without the table nothing else can run here; the other engines and shapes still can.
                        break
                    if workload.phase == "cold" or ctx.con is None:
                        ctx.con = engine.connect(ctx.db_path)
        finally:
            if ctx.con is not None:
                try:
                    ctx.con.close()
                except Exception:
                    pass
            if not self.keep_data:
                engine.remove_files(ctx.db_path)
        return out

    @staticmethod
    def _recover(ctx: Ctx) -> None:
        """After a failed workload, make sure no transaction is left open."""
        if ctx.con is None:
            return
        try:
            ctx.con.execute("ROLLBACK")
        except Exception:
            pass

    def _storage_record(self, engine: Engine, size: SizeSpec, width: WidthSpec, path: Path) -> dict:
        size_bytes = engine.file_bytes(path)
        return {
            "engine": engine.spec.key, "size": size.key, "width": width.key,
            "rows": size.rows, "bytes": size_bytes, "bytes_per_row": round(size_bytes / size.rows, 1),
        }

    def _print_shape_table(self, engines: list[Engine], shape_results: dict[str, dict[str, dict]]) -> None:
        table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        table.add_column("workload")
        table.add_column("class")
        for engine in engines:
            table.add_column(engine.spec.label, justify="right")
        if len(engines) >= 2:
            table.add_column("faster", justify="right")
        for workload in WORKLOADS:
            cells = [shape_results.get(e.spec.key, {}).get(workload.key) for e in engines]
            if not any(cells):
                continue
            medians = [c.get("median") if c else None for c in cells]
            row = [workload.label, f"{workload.category} {workload.kind}", *[format_seconds(m) for m in medians]]
            if len(engines) >= 2:
                a, b = medians[0], medians[1]
                if a and b:
                    winner, ratio = (engines[1], a / b) if b < a else (engines[0], b / a)
                    row.append(f"{winner.spec.label} {ratio:,.1f}x")
                else:
                    row.append("-")
            table.add_row(*row)
        self.console.print(table)
        self.console.print()


def save_results(results: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"results-{results['run_id']}.json"
    text = json.dumps(results, indent=1, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    (out_dir / "latest.json").write_text(text, encoding="utf-8")
    return path
