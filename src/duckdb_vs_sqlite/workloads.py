"""The benchmark workloads.

Every workload belongs to one cell of a 2x2 grid - OLTP or OLAP, read or write -
and produces a list of timing samples in seconds. One sample is one unit of work:
a query, a single-row commit, a batch, a whole bulk load.

Timing always covers ``execute()`` *and* ``fetchall()``: what an application pays to
get its answer back as Python objects.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter, perf_counter_ns

from .config import BenchConfig
from .datagen import CATEGORIES, STATUSES, Staging
from .engines import Engine

MIN_OPS = 20  # never stop an OLTP loop on budget before this many operations
MIN_REPEATS = 2  # never stop a repeated statement on budget before this many runs


@dataclass
class Ctx:
    """Everything a workload needs for one engine on one database."""

    engine: Engine
    cfg: BenchConfig
    staging: Staging
    db_path: Path
    rows: int
    width: str
    customers: int
    con: object = None
    pool: list[tuple] = field(default_factory=list)
    _pool_pos: int = 0
    _write_ids: list[int] = field(default_factory=list)

    def rng(self, salt: str) -> random.Random:
        # Seeded by workload and database shape only - never by engine - so every
        # engine sees the identical sequence of keys and parameters.
        return random.Random(f"{self.cfg.seed}:{self.rows}:{self.width}:{salt}")

    def take_pool(self, n: int) -> list[tuple]:
        """Reserve the next ``n`` not-yet-inserted rows (ids above the base table)."""
        rows = self.pool[self._pool_pos : self._pool_pos + n]
        self._pool_pos += n
        return rows

    @property
    def write_ops(self) -> int:
        return max(MIN_OPS, min(self.cfg.oltp_write_ops, self.rows // 4))

    def write_ids(self, slot: int) -> list[int]:
        """Disjoint sets of existing ids for update (0), delete (1) and transaction (2) workloads."""
        n = self.write_ops
        if not self._write_ids:
            self._write_ids = self.rng("write-ids").sample(range(1, self.rows + 1), 3 * n)
        return self._write_ids[slot * n : (slot + 1) * n]


@dataclass
class Outcome:
    samples: list[float]
    rows_per_op: float | None = None  # rows written/returned by one sample, for rows/s
    result_rows: int | None = None  # rows returned by the query (cross-engine sanity check)


@dataclass(frozen=True)
class Workload:
    key: str
    label: str
    category: str  # "OLTP" | "OLAP"
    kind: str  # "read" | "write"
    unit: str  # what one timing sample covers
    description: str
    sql: dict[str, str]  # dialect ("all", "sqlite", "duckdb") -> statement shown in the dashboard
    run: Callable[[Ctx], Outcome]
    phase: str = "main"  # "load" | "index" | "cold" | "main" - controls ordering in the runner

    def meta(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "category": self.category,
            "kind": self.kind,
            "unit": self.unit,
            "description": self.description,
            "sql": self.sql,
        }


def _sql(sql: dict[str, str], engine: Engine) -> str:
    return sql.get(engine.kind) or sql["all"]


# ---------------------------------------------------------------------------
# Timing loops
# ---------------------------------------------------------------------------
def timed_ops(ctx: Ctx, sql: str, params: list[tuple], warmup: list[tuple] = ()) -> tuple[list[float], int]:
    """Run one statement per parameter tuple, timing each individually."""
    execute = ctx.con.execute
    for p in warmup:
        execute(sql, p).fetchall()
    budget_ns = int(ctx.cfg.budget_s * 1e9)
    samples: list[int] = []
    last_rows = 0
    start = perf_counter_ns()
    for p in params:
        t0 = perf_counter_ns()
        rows = execute(sql, p).fetchall()
        t1 = perf_counter_ns()
        samples.append(t1 - t0)
        last_rows = len(rows)
        if t1 - start > budget_ns and len(samples) >= MIN_OPS:
            break
    return [s / 1e9 for s in samples], last_rows


def timed_repeats(
    ctx: Ctx,
    op: Callable[[int], object],
    *,
    repeats: int | None = None,
    warmup: int = 1,
    setup: Callable[[int], None] | None = None,
) -> list[float]:
    """Time ``op(i)`` up to ``repeats`` times, stopping early once the budget is spent."""
    repeats = repeats or ctx.cfg.repeats
    start = perf_counter()
    for _ in range(warmup):
        op(0)
    samples: list[float] = []
    for i in range(repeats):
        if setup:
            setup(i)
        t0 = perf_counter_ns()
        op(i)
        samples.append((perf_counter_ns() - t0) / 1e9)
        if perf_counter() - start > ctx.cfg.budget_s and len(samples) >= MIN_REPEATS:
            break
    return samples


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------
def olap_read(key: str, label: str, description: str, sql: dict[str, str] | str,
              params: Callable[[Ctx], tuple] | None = None,
              rows_per_op: Callable[[Ctx], float] | None = None) -> Workload:
    sql_map = {"all": sql} if isinstance(sql, str) else sql

    def run(ctx: Ctx) -> Outcome:
        statement = _sql(sql_map, ctx.engine)
        p = params(ctx) if params else ()
        seen = {}

        def op(_i: int) -> None:
            seen["n"] = len(ctx.con.execute(statement, p).fetchall())

        samples = timed_repeats(ctx, op)
        return Outcome(samples, rows_per_op(ctx) if rows_per_op else None, seen.get("n"))

    return Workload(key, label, "OLAP", "read", "query", description, sql_map, run)


def oltp_read(key: str, label: str, description: str, sql: str,
              params: Callable[[Ctx, random.Random], tuple]) -> Workload:
    def run(ctx: Ctx) -> Outcome:
        rng = ctx.rng(key)
        warm = [params(ctx, rng) for _ in range(MIN_OPS)]
        plist = [params(ctx, rng) for _ in range(ctx.cfg.oltp_read_ops)]
        samples, n = timed_ops(ctx, sql, plist, warm)
        return Outcome(samples, None, None)

    return Workload(key, label, "OLTP", "read", "lookup", description, {"all": sql}, run)


# ---------------------------------------------------------------------------
# OLTP - reads
# ---------------------------------------------------------------------------
POINT_SELECT_SQL = "SELECT * FROM orders WHERE id = ?"


def _run_cold_start(ctx: Ctx) -> Outcome:
    keys = [ctx.rng("cold").randint(1, ctx.rows) for _ in range(64)]

    def op(i: int) -> None:
        con = ctx.engine.connect(ctx.db_path)
        try:
            con.execute(POINT_SELECT_SQL, (keys[i % len(keys)],)).fetchall()
        finally:
            con.close()

    return Outcome(timed_repeats(ctx, op, repeats=max(10, ctx.cfg.repeats)))


OLTP_READS = [
    Workload(
        "cold_start", "Open database + first lookup", "OLTP", "read", "open",
        "Open a fresh connection, apply the engine settings, fetch one row by primary key, close. "
        "The operating system's file cache is warm; this measures driver and engine start-up cost.",
        {"all": "-- connect(), then:\n" + POINT_SELECT_SQL}, _run_cold_start, phase="cold",
    ),
    oltp_read(
        "point_select", "Point lookup by primary key",
        "Fetch one full row by primary key - the canonical OLTP read.",
        POINT_SELECT_SQL, lambda ctx, rng: (rng.randint(1, ctx.rows),),
    ),
    oltp_read(
        "index_lookup", "Secondary-index lookup",
        "Fetch all orders of one customer (about 20 rows) through the secondary index on customer_id.",
        "SELECT id, amount, status, created_at FROM orders WHERE customer_id = ?",
        lambda ctx, rng: (rng.randint(1, ctx.customers),),
    ),
    oltp_read(
        "range_page", "Paged range scan (50 rows)",
        "Keyset pagination: 50 consecutive rows in primary-key order starting at a random key.",
        "SELECT id, customer_id, amount, status FROM orders WHERE id >= ? ORDER BY id LIMIT 50",
        lambda ctx, rng: (rng.randint(1, max(1, ctx.rows - 50)),),
    ),
    oltp_read(
        "point_join", "Point lookup with join",
        "Fetch one order by primary key together with its customer record.",
        "SELECT o.id, o.amount, o.status, c.name, c.region\n"
        "FROM orders o JOIN customers c ON c.id = o.customer_id\nWHERE o.id = ?",
        lambda ctx, rng: (rng.randint(1, ctx.rows),),
    ),
]


# ---------------------------------------------------------------------------
# OLTP - writes (every operation is its own durable commit)
# ---------------------------------------------------------------------------
def _run_single_insert(ctx: Ctx) -> Outcome:
    rows = ctx.take_pool(ctx.cfg.oltp_write_ops)
    samples, _ = timed_ops(ctx, ctx.engine.insert_sql(ctx.width), rows)
    return Outcome(samples, 1)


POINT_UPDATE_SQL = "UPDATE orders SET status = ?, quantity = quantity + 1 WHERE id = ?"
POINT_DELETE_SQL = "DELETE FROM orders WHERE id = ?"


def _run_point_update(ctx: Ctx) -> Outcome:
    rng = ctx.rng("point_update")
    params = [(rng.choice(STATUSES), i) for i in ctx.write_ids(0)]
    samples, _ = timed_ops(ctx, POINT_UPDATE_SQL, params)
    return Outcome(samples, 1)


def _run_point_delete(ctx: Ctx) -> Outcome:
    samples, _ = timed_ops(ctx, POINT_DELETE_SQL, [(i,) for i in ctx.write_ids(1)])
    return Outcome(samples, 1)


TXN_SQL = (
    "BEGIN;\n"
    "SELECT amount, quantity FROM orders WHERE id = ?;\n"
    "UPDATE orders SET amount = ?, quantity = ? WHERE id = ?;\n"
    "INSERT INTO orders VALUES (?, ...);\n"
    "COMMIT;"
)


def _run_txn_mixed(ctx: Ctx) -> Outcome:
    ids = ctx.write_ids(2)
    new_rows = ctx.take_pool(len(ids))
    insert_sql = ctx.engine.insert_sql(ctx.width)
    execute = ctx.con.execute
    budget_ns = int(ctx.cfg.budget_s * 1e9)
    samples: list[int] = []
    start = perf_counter_ns()
    for order_id, new_row in zip(ids, new_rows):
        t0 = perf_counter_ns()
        execute("BEGIN")
        found = execute("SELECT amount, quantity FROM orders WHERE id = ?", (order_id,)).fetchall()
        amount, quantity = found[0] if found else (0.0, 0)
        execute("UPDATE orders SET amount = ?, quantity = ? WHERE id = ?",
                (amount + 1.0, quantity + 1, order_id)).fetchall()
        execute(insert_sql, new_row).fetchall()
        execute("COMMIT")
        t1 = perf_counter_ns()
        samples.append(t1 - t0)
        if t1 - start > budget_ns and len(samples) >= MIN_OPS:
            break
    return Outcome([s / 1e9 for s in samples], 2)


def _run_batch_insert(ctx: Ctx) -> Outcome:
    n = ctx.cfg.batch_rows
    batches = [ctx.take_pool(n) for _ in range(5)]
    insert_sql = ctx.engine.insert_sql(ctx.width)

    def op(i: int) -> None:
        ctx.con.execute("BEGIN")
        ctx.con.executemany(insert_sql, batches[i])
        ctx.con.execute("COMMIT")

    return Outcome(timed_repeats(ctx, op, repeats=len(batches), warmup=0), n)


OLTP_WRITES = [
    Workload(
        "single_insert", "Single-row insert", "OLTP", "write", "commit",
        "Insert one row and commit it durably, one transaction per row.",
        {"all": "INSERT INTO orders VALUES (?, ...)  -- autocommit"}, _run_single_insert,
    ),
    Workload(
        "point_update", "Single-row update", "OLTP", "write", "commit",
        "Update two columns of one row found by primary key; one durable commit per update.",
        {"all": POINT_UPDATE_SQL + "  -- autocommit"}, _run_point_update,
    ),
    Workload(
        "point_delete", "Single-row delete", "OLTP", "write", "commit",
        "Delete one row by primary key; one durable commit per delete.",
        {"all": POINT_DELETE_SQL + "  -- autocommit"}, _run_point_delete,
    ),
    Workload(
        "txn_mixed", "Read-modify-write transaction", "OLTP", "write", "transaction",
        "A small multi-statement transaction: read a row, update it, insert a new row, commit.",
        {"all": TXN_SQL}, _run_txn_mixed,
    ),
    Workload(
        "batch_insert", "Batch insert via executemany()", "OLTP", "write", "batch",
        "Insert a batch of rows with the driver's executemany() in one transaction - the same "
        "row-at-a-time API on both engines. (Batch size is in the run configuration.)",
        {"all": "BEGIN;\nINSERT INTO orders VALUES (?, ...);  -- executemany(batch)\nCOMMIT;"},
        _run_batch_insert,
    ),
]


# ---------------------------------------------------------------------------
# OLAP - reads
# ---------------------------------------------------------------------------
def _fetch_rows_n(ctx: Ctx) -> int:
    return min(5000, ctx.rows)


OLAP_READS = [
    olap_read(
        "agg_full", "Full-table aggregate",
        "Scan every row and compute a handful of aggregates, no filter.",
        "SELECT count(*), sum(amount), avg(quantity), min(created_at), max(created_at)\nFROM orders",
    ),
    olap_read(
        "agg_filter", "Filtered aggregate",
        "Aggregate about 10% of the table selected by a filter on unindexed columns.",
        "SELECT count(*), sum(amount), avg(quantity)\nFROM orders\nWHERE status = 'returned' AND amount > 500",
    ),
    olap_read(
        "group_low", "Group by (20 groups)",
        "Group the whole table by a low-cardinality column.",
        "SELECT category, count(*), sum(amount), avg(amount)\nFROM orders\nGROUP BY category\nORDER BY 3 DESC",
    ),
    olap_read(
        "group_high", "Group by (many groups) + top 100",
        "Group by a high-cardinality column (one group per ~20 rows) and keep the 100 largest.",
        "SELECT customer_id, count(*), sum(amount) AS total\nFROM orders\nGROUP BY customer_id\nORDER BY total DESC\nLIMIT 100",
    ),
    olap_read(
        "join_agg", "Join + aggregate",
        "Join the fact table to the customer dimension and aggregate by two dimension attributes.",
        "SELECT c.region, c.segment, count(*), sum(o.amount)\nFROM orders o JOIN customers c ON c.id = o.customer_id\n"
        "GROUP BY c.region, c.segment\nORDER BY 1, 2",
    ),
    olap_read(
        "time_series", "Monthly time series",
        "Bucket every order by calendar month using each engine's native date functions.",
        {
            "sqlite": "SELECT strftime('%Y-%m', created_at) AS month, count(*), sum(amount)\nFROM orders\nGROUP BY month\nORDER BY month",
            "duckdb": "SELECT date_trunc('month', created_at) AS month, count(*), sum(amount)\nFROM orders\nGROUP BY month\nORDER BY month",
        },
    ),
    olap_read(
        "top_n", "Top 100 by unindexed column",
        "Find the 100 largest orders: a top-N sort over the whole table.",
        "SELECT id, customer_id, amount\nFROM orders\nORDER BY amount DESC, id\nLIMIT 100",
    ),
    olap_read(
        "distinct_count", "Count distinct",
        "Count distinct values of a high- and a low-cardinality column.",
        "SELECT count(DISTINCT customer_id), count(DISTINCT product_id)\nFROM orders",
    ),
    olap_read(
        "window_rank", "Window function",
        "Rank every order within its customer and aggregate each customer's three largest.",
        "SELECT count(*), sum(amount)\nFROM (\n  SELECT amount, row_number() OVER (PARTITION BY customer_id ORDER BY amount DESC) AS rn\n"
        "  FROM orders\n) ranked\nWHERE rn <= 3",
    ),
    olap_read(
        "text_search", "Substring search",
        "Count rows whose note contains a word: an unanchored LIKE over a short text column.",
        "SELECT count(*)\nFROM orders\nWHERE note LIKE '%express%'",
    ),
    olap_read(
        "fetch_rows", "Export 5,000 full rows",
        "Materialise 5,000 complete rows (the whole table if it is smaller) as Python tuples with SELECT *. "
        "Row width matters most here.",
        "SELECT *\nFROM orders\nWHERE id BETWEEN ? AND ?",
        params=lambda ctx: (ctx.rows // 3 + 1, ctx.rows // 3 + _fetch_rows_n(ctx)) if ctx.rows > 5000 else (1, ctx.rows),
        rows_per_op=_fetch_rows_n,
    ),
]


# ---------------------------------------------------------------------------
# OLAP - writes
# ---------------------------------------------------------------------------
def _run_bulk_load(ctx: Ctx) -> Outcome:
    """Load the whole table into a brand-new database file, repeated while it is cheap."""
    samples: list[float] = []
    start = perf_counter()
    for _ in range(ctx.cfg.repeats):
        if ctx.con is not None:
            ctx.con.close()
        ctx.engine.remove_files(ctx.db_path)
        ctx.con = ctx.engine.connect(ctx.db_path)
        ctx.engine.create_schema(ctx.con, ctx.width)
        samples.append(ctx.engine.bulk_load_orders(ctx.con, ctx.staging))
        if perf_counter() - start > ctx.cfg.budget_s / 2:
            break
    ctx.engine.load_customers(ctx.con, ctx.staging)
    return Outcome(samples, ctx.rows)


CREATE_INDEX_SQL = "CREATE INDEX idx_orders_customer ON orders (customer_id)"


def _run_create_index(ctx: Ctx) -> Outcome:
    def setup(_i: int) -> None:
        ctx.con.execute("DROP INDEX IF EXISTS idx_orders_customer")

    def op(_i: int) -> None:
        ctx.con.execute(CREATE_INDEX_SQL)

    return Outcome(timed_repeats(ctx, op, warmup=0, setup=setup), ctx.rows)


CTAS_SQL = (
    "CREATE TABLE order_summary AS\n"
    "SELECT customer_id, category, count(*) AS orders, sum(amount) AS total, max(created_at) AS last_order\n"
    "FROM orders\nGROUP BY customer_id, category"
)


def _run_ctas(ctx: Ctx) -> Outcome:
    def setup(_i: int) -> None:
        ctx.con.execute("DROP TABLE IF EXISTS order_summary")

    def op(_i: int) -> None:
        ctx.con.execute(CTAS_SQL)

    samples = timed_repeats(ctx, op, warmup=0, setup=setup)
    ctx.con.execute("DROP TABLE IF EXISTS order_summary")
    return Outcome(samples)


BULK_UPDATE_SQL = "UPDATE orders\nSET amount = round(amount * 1.05, 2), status = 'returned'\nWHERE category = ?"
BULK_DELETE_SQL = "DELETE FROM orders\nWHERE product_id % 20 = ?"


def _run_bulk_update(ctx: Ctx) -> Outcome:
    def op(i: int) -> None:
        ctx.con.execute(BULK_UPDATE_SQL, (CATEGORIES[i % len(CATEGORIES)],)).fetchall()

    return Outcome(timed_repeats(ctx, op, warmup=0), ctx.rows / 20)


def _run_bulk_delete(ctx: Ctx) -> Outcome:
    def op(i: int) -> None:
        ctx.con.execute(BULK_DELETE_SQL, (i % 20,)).fetchall()

    return Outcome(timed_repeats(ctx, op, warmup=0), ctx.rows / 20)


OLAP_WRITES = [
    Workload(
        "bulk_load", "Bulk load (idiomatic path)", "OLAP", "write", "load",
        "Load the entire orders table into an empty database in one transaction, primary key in place, "
        "using each engine's fastest route from in-memory data: executemany() of Python tuples for SQLite, "
        "INSERT ... SELECT from Arrow tables for DuckDB.",
        {
            "sqlite": "BEGIN;\nINSERT INTO orders VALUES (?, ...);  -- executemany(), 50,000 tuples per call\nCOMMIT;",
            "duckdb": "BEGIN;\nINSERT INTO orders SELECT * FROM arrow_table;  -- ~50,000 rows per Arrow table\nCOMMIT;",
        },
        _run_bulk_load, phase="load",
    ),
    Workload(
        "create_index", "Create secondary index", "OLAP", "write", "statement",
        "Build a secondary index over the loaded table.",
        {"all": CREATE_INDEX_SQL}, _run_create_index, phase="index",
    ),
    Workload(
        "ctas", "Create table as select", "OLAP", "write", "statement",
        "Aggregate the fact table and persist the result as a new table.",
        {"all": CTAS_SQL}, _run_ctas,
    ),
    Workload(
        "bulk_update", "Bulk update (5% of rows)", "OLAP", "write", "statement",
        "Update two columns on every row of one category, about 5% of the table, in one statement.",
        {"all": BULK_UPDATE_SQL}, _run_bulk_update,
    ),
    Workload(
        "bulk_delete", "Bulk delete (5% of rows)", "OLAP", "write", "statement",
        "Delete about 5% of the table, scattered across it, in one statement.",
        {"all": BULK_DELETE_SQL}, _run_bulk_delete,
    ),
]

# Display order (dashboard): OLTP before OLAP, reads before writes.
WORKLOADS: list[Workload] = [*OLTP_READS, *OLTP_WRITES, *OLAP_READS, *OLAP_WRITES]
BY_KEY: dict[str, Workload] = {w.key: w for w in WORKLOADS}

# Execution order: load, index, cold start, then all reads before anything mutates the
# table, single-row writes next, and the statements that rewrite 5% of the table last.
EXECUTION_ORDER: list[str] = [
    "bulk_load", "create_index", "cold_start",
    *[w.key for w in OLAP_READS],
    *[w.key for w in OLTP_READS if w.phase == "main"],
    *[w.key for w in OLTP_WRITES],
    "ctas", "bulk_update", "bulk_delete",
]
assert sorted(EXECUTION_ORDER) == sorted(BY_KEY), "every workload must be scheduled exactly once"
