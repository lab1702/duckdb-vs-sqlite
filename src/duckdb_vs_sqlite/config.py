"""Benchmark configuration: database sizes, row widths, engine variants, presets."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace


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

    @property
    def extra_rows(self) -> int:
        """Rows generated beyond the base table, used as the pool for insert workloads."""
        return 2 * self.oltp_write_ops + 5 * self.batch_rows

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
            v = n / div
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
        rows = int(float(count.strip().replace("_", "")))
        if rows < 100:
            raise ValueError(f"database size must be at least 100 rows, got {rows}")
        key = (name or f"n{rows}").lower().replace(" ", "-")
        label = name.capitalize() if name else human_rows(rows)
        sizes.append(SizeSpec(key, label, rows))
    if not sizes:
        raise ValueError("no database sizes given")
    if len({s.key for s in sizes}) != len(sizes):
        raise ValueError("database size names must be unique")
    return sorted(sizes, key=lambda s: s.rows)


def make_config(preset: str = "standard", **overrides) -> BenchConfig:
    base = PRESETS[preset]
    clean = {k: v for k, v in overrides.items() if v is not None}
    return replace(base, **clean)
