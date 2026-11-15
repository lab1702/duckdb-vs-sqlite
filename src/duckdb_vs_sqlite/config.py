"""Benchmark configuration: database sizes, row widths, engine variants, presets."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace

MIN_OPS = 20  # never stop an OLTP loop on budget before this many operations
MIN_ROWS = 100  # smallest table the write workloads can draw their disjoint id sets from
SQLITE_SYNCHRONOUS = ("FULL", "NORMAL", "OFF")
SCHEMA_VERSION = 1  # of the results JSON


@dataclass(frozen=True)
class SizeSpec:
    key: str
    label: str
    rows: int


@dataclass(frozen=True)
class WidthSpec:
    key: str
    label: str
    description: str


@dataclass(frozen=True)
class EngineSpec:
    key: str
    label: str
    short: str  # compact tag used inside dense dashboard cells
    kind: str  # "sqlite" | "duckdb" - selects SQL dialect and load path
    threads: int | None = None  # DuckDB only; None = engine default (all cores)


WIDTHS: dict[str, WidthSpec] = {
    "narrow": WidthSpec(
        "narrow",
        "Narrow rows",
        "9 columns, roughly 100 bytes per row (ids, a few numbers, short strings, a timestamp).",
    ),
    "wide": WidthSpec(
        "wide",
        "Wide rows",
        "21 columns, roughly 1.3 KB per row (the narrow columns plus 8 doubles and 4 text columns of ~250 characters).",
    ),
}

ENGINES: dict[str, EngineSpec] = {
    "sqlite": EngineSpec("sqlite", "SQLite", "SQLite", "sqlite"),
    "duckdb": EngineSpec("duckdb", "DuckDB", "DuckDB", "duckdb"),
    "duckdb-1t": EngineSpec("duckdb-1t", "DuckDB (1 thread)", "Duck·1T", "duckdb", threads=1),
}


class FieldError(ValueError):
    """A BenchConfig field holds an unusable value. Reads as "<field> <problem>"."""

    def __init__(self, field: str, problem: str):
        super().__init__(f"{field} {problem}")
        self.field = field
        self.problem = problem


def check_sizes(sizes: list[SizeSpec]) -> None:
    """The rules every list of database sizes must meet, however it was built."""
    if not sizes:
        raise ValueError("no database sizes given")
    for size in sizes:
        if not isinstance(size.rows, int) or size.rows < MIN_ROWS:
            raise ValueError(f"database size must be at least {MIN_ROWS} rows, got {size.rows!r}")
    if len({s.key for s in sizes}) != len(sizes):
        raise ValueError("database size names must be unique")


@dataclass
class BenchConfig:
    sizes: list[SizeSpec]
    widths: list[WidthSpec] = field(default_factory=lambda: [WIDTHS["narrow"], WIDTHS["wide"]])
    engines: list[EngineSpec] = field(default_factory=lambda: [ENGINES["sqlite"], ENGINES["duckdb"]])
    preset: str = "standard"
    repeats: int = 5  # timed repetitions of each OLAP query / bulk statement
    oltp_read_ops: int = 2000  # point reads per OLTP read workload
    oltp_write_ops: int = 500  # single-row commits per OLTP write workload
    batch_rows: int = 1000  # rows per executemany batch
    budget_s: float = 15.0  # soft time budget per workload, per engine, per database
    chunk_rows: int = 50_000  # rows handed to the engine per bulk-load call
    seed: int = 20260101
    sqlite_synchronous: str = "FULL"  # FULL = fsync on every commit, matching DuckDB's durability
    sqlite_cache_mb: int = 256

    def __post_init__(self) -> None:
        # One rule set for the CLI and for make_config() callers alike.
        check_sizes(self.sizes)
        # Smallest first: the dashboard reads the ends of the list as the size range.
        self.sizes = sorted(self.sizes, key=lambda s: s.rows)
        for name in ("widths", "engines"):
            if not getattr(self, name):
                raise ValueError(f"no {name} given")
        for name in ("repeats", "oltp_read_ops", "oltp_write_ops", "batch_rows", "chunk_rows", "sqlite_cache_mb"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 1:
                raise FieldError(name, f"must be a whole number of at least 1, got {value!r}")
        if not (isinstance(self.budget_s, (int, float)) and math.isfinite(self.budget_s) and self.budget_s > 0):
            raise FieldError("budget_s", f"must be a number greater than 0, got {self.budget_s!r}")
        if self.sqlite_synchronous not in SQLITE_SYNCHRONOUS:
            raise FieldError(
                "sqlite_synchronous", f"must be one of {', '.join(SQLITE_SYNCHRONOUS)}, got {self.sqlite_synchronous!r}"
            )

    @property
    def extra_rows(self) -> int:
        """Rows generated beyond the base table, used as the pool for insert workloads."""
        # single_insert takes oltp_write_ops rows, txn_mixed at most max(MIN_OPS, oltp_write_ops),
        # batch_insert five batches.
        return self.oltp_write_ops + max(MIN_OPS, self.oltp_write_ops) + 5 * self.batch_rows

    def to_dict(self) -> dict:
        return asdict(self)


PRESETS: dict[str, BenchConfig] = {
    # A couple of minutes or less: sanity check / CI.
    "quick": BenchConfig(
        sizes=[SizeSpec("small", "Small", 2_000), SizeSpec("large", "Large", 50_000)],
        preset="quick",
        repeats=3,
        oltp_read_ops=300,
        oltp_write_ops=100,
        batch_rows=500,
        budget_s=4.0,
    ),
    # The default: a database that fits in any cache vs one that does not fit in SQLite's.
    "standard": BenchConfig(
        sizes=[SizeSpec("small", "Small", 10_000), SizeSpec("large", "Large", 1_000_000)],
        preset="standard",
    ),
    # Three points on the size axis; the wide 5M-row database is several GB on disk.
    "full": BenchConfig(
        sizes=[
            SizeSpec("small", "Small", 10_000),
            SizeSpec("medium", "Medium", 250_000),
            SizeSpec("large", "Large", 5_000_000),
        ],
        preset="full",
        budget_s=30.0,
    ),
}


def human_rows(n: int) -> str:
    for div, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= div:
            v = round(n / div, 1)  # round first: 999,999 is "1000K", not "1000.0K"
            return f"{v:.0f}{suffix}" if v == int(v) else f"{v:.1f}{suffix}"
    return str(n)


def parse_sizes(text: str) -> list[SizeSpec]:
    """Parse ``small=10000,large=1000000`` or bare ``10000,1000000``."""
    sizes: list[SizeSpec] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            name, _, count = part.partition("=")
            name = name.strip()
        else:
            name, count = "", part
        try:
            value = float(count.strip().replace("_", ""))
        except ValueError:
            value = math.nan
        if not value.is_integer():  # not a number, NaN, infinite or fractional
            raise ValueError(f"database size must be a whole number of rows, got {count.strip()!r}")
        rows = int(value)
        key = (name or f"n{rows}").lower().replace(" ", "-")
        label = name.capitalize() if name else human_rows(rows)
        sizes.append(SizeSpec(key, label, rows))
    check_sizes(sizes)
    return sorted(sizes, key=lambda s: s.rows)


def make_config(preset: str = "standard", **overrides) -> BenchConfig:
    base = PRESETS[preset]
    clean = {k: v for k, v in overrides.items() if v is not None}
    return replace(base, **clean)
