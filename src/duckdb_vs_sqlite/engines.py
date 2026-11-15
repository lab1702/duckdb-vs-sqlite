"""Thin adapters over the two engines.

Both Python drivers speak close enough to DB-API (``execute``, ``executemany``,
``fetchall``, ``?`` placeholders, explicit ``BEGIN``/``COMMIT``) that the workloads
can share code; what differs - connection setup, DDL types and the idiomatic bulk
load path - lives here.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from time import perf_counter

import duckdb

from .config import BenchConfig, EngineSpec
from .datagen import CUSTOMER_COLUMNS, Staging, order_columns


class Engine:
    kind = ""
    file_suffix = ""

    def __init__(self, spec: EngineSpec, cfg: BenchConfig):
        self.spec = spec
        self.cfg = cfg

    # -- to be provided by subclasses ----------------------------------------
    def connect(self, path: Path):
        raise NotImplementedError

    def settings(self) -> dict[str, str]:
        raise NotImplementedError

    def bulk_load_orders(self, con, staging: Staging) -> float:
        raise NotImplementedError

    def load_customers(self, con, staging: Staging) -> None:
        raise NotImplementedError

    def finalize_load(self, con) -> None:
        """Checkpoint and refresh statistics after the initial load (not timed)."""
        raise NotImplementedError

    def checkpoint(self, con) -> None:
        raise NotImplementedError

    # -- shared ---------------------------------------------------------------
    def _type_index(self) -> int:
        return 1 if self.kind == "sqlite" else 2

    def create_schema(self, con, width: str) -> None:
        idx = self._type_index()
        con.execute("DROP TABLE IF EXISTS orders")
        con.execute("DROP TABLE IF EXISTS customers")
        cols = ", ".join(f"{c[0]} {c[idx]}" for c in order_columns(width))
        con.execute(f"CREATE TABLE orders ({cols})")
        cols = ", ".join(f"{c[0]} {c[idx]}" for c in CUSTOMER_COLUMNS)
        con.execute(f"CREATE TABLE customers ({cols})")

    def insert_sql(self, width: str) -> str:
        placeholders = ", ".join("?" for _ in order_columns(width))
        return f"INSERT INTO orders VALUES ({placeholders})"

    def db_path(self, workdir: Path) -> Path:
        return workdir / f"bench-{self.spec.key}{self.file_suffix}"

    # The file helpers depend only on the engine kind, so they also work on the class.
    @classmethod
    def file_bytes(cls, path: Path) -> int:
        total = 0
        for candidate in (path, *cls.sidecar_files(path)):
            if candidate.exists():
                total += candidate.stat().st_size
        return total

    @classmethod
    def sidecar_files(cls, path: Path) -> list[Path]:
        return []

    @classmethod
    def spill_dirs(cls, path: Path) -> list[Path]:
        """Directories the engine itself creates next to the database for temporary data."""
        return []

    @classmethod
    def remove_files(cls, path: Path) -> None:
        """Delete a database with everything the engine put beside it. Never raises."""
        for candidate in (path, *cls.sidecar_files(path)):
            try:
                candidate.unlink()
            except OSError:
                pass  # missing, or (Windows) still locked: the next load drops the tables anyway
        for directory in cls.spill_dirs(path):
            shutil.rmtree(directory, ignore_errors=True)


class SqliteEngine(Engine):
    kind = "sqlite"
    file_suffix = ".sqlite"

    def connect(self, path: Path):
        # isolation_level=None puts the driver in autocommit mode: every statement
        # commits on its own unless the workload issues an explicit BEGIN.
        con = sqlite3.connect(str(path), isolation_level=None)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(f"PRAGMA synchronous={self.cfg.sqlite_synchronous}")
        con.execute(f"PRAGMA cache_size={-1024 * self.cfg.sqlite_cache_mb}")
        con.execute("PRAGMA temp_store=MEMORY")
        return con

    def settings(self) -> dict[str, str]:
        return {
            "driver": "sqlite3 (Python standard library)",
            "journal_mode": "WAL",
            "synchronous": f"{self.cfg.sqlite_synchronous} (fsync on every commit)"
            if self.cfg.sqlite_synchronous == "FULL"
            else self.cfg.sqlite_synchronous,
            "cache_size": f"{self.cfg.sqlite_cache_mb} MB",
            "temp_store": "MEMORY",
            "threads": "1 (SQLite executes a query on a single thread)",
            "bulk load path": "executemany() of Python tuples inside one transaction",
        }

    def bulk_load_orders(self, con, staging: Staging) -> float:
        sql = self.insert_sql(staging.width)
        elapsed = 0.0
        t0 = perf_counter()
        con.execute("BEGIN")
        elapsed += perf_counter() - t0
        for chunk in staging.iter_tuples(self.cfg.chunk_rows):
            t0 = perf_counter()
            con.executemany(sql, chunk)
            elapsed += perf_counter() - t0
        t0 = perf_counter()
        con.execute("COMMIT")
        elapsed += perf_counter() - t0
        return elapsed

    def load_customers(self, con, staging: Staging) -> None:
        con.execute("BEGIN")
        con.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?)", staging.customer_tuples())
        con.execute("COMMIT")

    def finalize_load(self, con) -> None:
        con.execute("ANALYZE")
        self.checkpoint(con)

    def checkpoint(self, con) -> None:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()

    @classmethod
    def sidecar_files(cls, path: Path) -> list[Path]:
        return [path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")]


class DuckDBEngine(Engine):
    kind = "duckdb"
    file_suffix = ".duckdb"

    def connect(self, path: Path):
        con = duckdb.connect(str(path))
        if self.spec.threads:
            con.execute(f"SET threads = {int(self.spec.threads)}")
        return con

    def settings(self) -> dict[str, str]:
        probe = duckdb.connect()
        try:
            default_threads, memory_limit = probe.execute(
                "SELECT current_setting('threads'), current_setting('memory_limit')"
            ).fetchone()
        finally:
            probe.close()
        return {
            "driver": "duckdb (PyPI)",
            "threads": str(self.spec.threads or f"{default_threads} (default: all cores)"),
            "memory_limit": f"{memory_limit} (default)",
            "durability": "WAL, fsync on every commit (default)",
            "bulk load path": "INSERT ... SELECT from Arrow tables inside one transaction",
        }

    def bulk_load_orders(self, con, staging: Staging) -> float:
        elapsed = 0.0
        t0 = perf_counter()
        con.execute("BEGIN")
        elapsed += perf_counter() - t0
        for table in staging.iter_arrow(self.cfg.chunk_rows):
            t0 = perf_counter()
            con.register("_bench_batch", table)
            con.execute("INSERT INTO orders SELECT * FROM _bench_batch")
            con.unregister("_bench_batch")
            elapsed += perf_counter() - t0
        t0 = perf_counter()
        con.execute("COMMIT")
        elapsed += perf_counter() - t0
        return elapsed

    def load_customers(self, con, staging: Staging) -> None:
        table = staging.customer_arrow()
        con.register("_bench_customers", table)
        con.execute(
            "INSERT INTO customers SELECT id, name, region, segment, CAST(signup_date AS DATE) FROM _bench_customers"
        )
        con.unregister("_bench_customers")

    def finalize_load(self, con) -> None:
        self.checkpoint(con)

    def checkpoint(self, con) -> None:
        con.execute("CHECKPOINT")

    @classmethod
    def sidecar_files(cls, path: Path) -> list[Path]:
        return [path.with_name(path.name + ".wal")]

    @classmethod
    def spill_dirs(cls, path: Path) -> list[Path]:
        # Larger-than-memory operators spill here; a killed run leaves it behind.
        return [path.with_name(path.name + ".tmp")]


def make_engine(spec: EngineSpec, cfg: BenchConfig) -> Engine:
    if spec.kind == "sqlite":
        return SqliteEngine(spec, cfg)
    if spec.kind == "duckdb":
        return DuckDBEngine(spec, cfg)
    raise ValueError(f"unknown engine kind: {spec.kind}")
