"""Deterministic synthetic data.

The dataset is an ``orders`` fact table plus a small ``customers`` dimension. It is
generated once per (size, width) into a scratch DuckDB file - purely because that is
the fastest portable way to produce millions of rows - and then streamed to *both*
engines from there, so each engine loads byte-for-byte the same rows. Reading from
the scratch file is never part of a timed section.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pyarrow as pa

CATEGORIES = [
    "apparel", "automotive", "baby", "beauty", "books", "electronics", "garden",
    "grocery", "health", "home", "jewelry", "kitchen", "music", "office", "outdoors",
    "pets", "shoes", "sports", "tools", "toys",
]
STATUSES = ["pending", "paid", "shipped", "delivered", "returned"]
REGIONS = ["north", "south", "east", "west", "central", "emea", "apac", "latam"]
SEGMENTS = ["consumer", "small-business", "enterprise", "public-sector"]
WORDS = [
    "account", "address", "arrived", "billing", "bundle", "cancel", "carrier", "change",
    "charge", "check", "confirm", "contact", "courier", "credit", "customer", "damaged",
    "delay", "deliver", "deposit", "discount", "dispatch", "door", "email", "exchange",
    "express", "fragile", "freight", "gift", "handle", "hold", "invoice", "label",
    "leave", "notify", "order", "package", "pallet", "payment", "phone", "pickup",
    "please", "priority", "promo", "receipt", "refund", "replace", "request", "return",
    "review", "route", "sample", "schedule", "signature", "standard", "stock", "store",
    "support", "tracking", "update", "urgent", "verify", "voucher", "warehouse", "weekend",
]

# (name, sqlite type, duckdb type)
NARROW_COLUMNS: list[tuple[str, str, str]] = [
    ("id", "INTEGER PRIMARY KEY", "BIGINT PRIMARY KEY"),
    ("customer_id", "INTEGER NOT NULL", "INTEGER NOT NULL"),
    ("product_id", "INTEGER NOT NULL", "INTEGER NOT NULL"),
    ("category", "TEXT NOT NULL", "VARCHAR NOT NULL"),
    ("status", "TEXT NOT NULL", "VARCHAR NOT NULL"),
    ("quantity", "INTEGER NOT NULL", "INTEGER NOT NULL"),
    ("amount", "REAL NOT NULL", "DOUBLE NOT NULL"),
    ("created_at", "TEXT NOT NULL", "TIMESTAMP NOT NULL"),
    ("note", "TEXT", "VARCHAR"),
]
WIDE_EXTRA_COLUMNS: list[tuple[str, str, str]] = [
    *[(f"metric_{i}", "REAL", "DOUBLE") for i in range(1, 9)],
    *[(f"text_{i}", "TEXT", "VARCHAR") for i in range(1, 5)],
]
CUSTOMER_COLUMNS: list[tuple[str, str, str]] = [
    ("id", "INTEGER PRIMARY KEY", "INTEGER PRIMARY KEY"),
    ("name", "TEXT NOT NULL", "VARCHAR NOT NULL"),
    ("region", "TEXT NOT NULL", "VARCHAR NOT NULL"),
    ("segment", "TEXT NOT NULL", "VARCHAR NOT NULL"),
    ("signup_date", "TEXT NOT NULL", "DATE NOT NULL"),
]

TS_FORMAT = "%Y-%m-%d %H:%M:%S"
TIME_SPAN_SECONDS = 3 * 365 * 24 * 3600  # orders are spread over three years


def order_columns(width: str) -> list[tuple[str, str, str]]:
    return NARROW_COLUMNS + (WIDE_EXTRA_COLUMNS if width == "wide" else [])


def n_customers(rows: int) -> int:
    return max(100, rows // 20)


def _sql_list(values: list[str]) -> str:
    return "[" + ", ".join(f"'{v}'" for v in values) + "]"


def _pick(values: list[str], salt: int, seed: int) -> str:
    return f"{_sql_list(values)}[(hash(i * {salt} + {seed}) % {len(values)} + 1)::BIGINT]"


def _words(n_words: int, salt: int, seed: int) -> str:
    return (
        f"array_to_string([{_sql_list(WORDS)}[(hash(i * {salt} + k + {seed}) % {len(WORDS)} + 1)::BIGINT] "
        f"for k in range({n_words})], ' ')"
    )


class Staging:
    """Scratch DuckDB file holding the generated rows for one (size, width) cell."""

    def __init__(self, path: Path, rows: int, width: str, extra_rows: int, seed: int):
        self.path = path
        self.rows = rows
        self.width = width
        self.extra_rows = extra_rows
        self.seed = seed
        self.customers_n = n_customers(rows)
        self.columns = order_columns(width)
        self._con = duckdb.connect(str(path))

    # -- generation ---------------------------------------------------------
    def build(self) -> None:
        total = self.rows + self.extra_rows
        s = self.seed
        step = max(1, TIME_SPAN_SECONDS // total)
        cols = [
            "i::BIGINT AS id",
            f"(hash(i * 7 + {s}) % {self.customers_n} + 1)::INTEGER AS customer_id",
            f"(hash(i * 11 + {s}) % 1000 + 1)::INTEGER AS product_id",
            f"{_pick(CATEGORIES, 13, s)} AS category",
            f"{_pick(STATUSES, 17, s)} AS status",
            f"(hash(i * 19 + {s}) % 10 + 1)::INTEGER AS quantity",
            f"round((hash(i * 23 + {s}) % 100000) / 100.0 + 1, 2)::DOUBLE AS amount",
            # Roughly time-ordered with jitter, like an append-mostly table in real life.
            f"(TIMESTAMP '2022-01-01 00:00:00' + to_seconds((i * {step} + hash(i * 29 + {s}) % 3600)::BIGINT)) AS created_at",
            f"{_words(4, 31, s)} AS note",
        ]
        if self.width == "wide":
            cols += [
                f"round((hash(i * {37 + 2 * m} + {s}) % 1000000) / 1000.0, 3)::DOUBLE AS metric_{m}"
                for m in range(1, 9)
            ]
            cols += [f"{_words(36, 1000 * t, s)} AS text_{t}" for t in range(1, 5)]
        self._con.execute(
            f"CREATE OR REPLACE TABLE orders_src AS SELECT {', '.join(cols)} FROM range(1, {total + 1}) t(i)"
        )
        self._con.execute(
            f"""CREATE OR REPLACE TABLE customers_src AS SELECT
                i::INTEGER AS id,
                'Customer ' || lpad(i::VARCHAR, 8, '0') AS name,
                {_pick(REGIONS, 41, s)} AS region,
                {_pick(SEGMENTS, 43, s)} AS segment,
                strftime(DATE '2015-01-01' + (hash(i * 47 + {s}) % 2500)::INTEGER, '%Y-%m-%d') AS signup_date
            FROM range(1, {self.customers_n + 1}) t(i)"""
        )

    # -- readers (never timed) ----------------------------------------------
    def _select_list(self, timestamps_as_text: bool) -> str:
        names = []
        for name, _, _ in self.columns:
            if name == "created_at" and timestamps_as_text:
                names.append(f"strftime(created_at, '{TS_FORMAT}') AS created_at")
            else:
                names.append(name)
        return ", ".join(names)

    def iter_tuples(self, chunk_rows: int) -> Iterator[list[tuple]]:
        """Base rows as Python tuples (timestamps as ISO text), in id order."""
        cur = self._con.cursor()
        cur.execute(f"SELECT {self._select_list(True)} FROM orders_src WHERE id <= {self.rows}")
        while True:
            chunk = cur.fetchmany(chunk_rows)
            if not chunk:
                break
            yield chunk
        cur.close()

    def iter_arrow(self, chunk_rows: int) -> Iterator[pa.Table]:
        """Base rows as Arrow tables of about ``chunk_rows`` rows."""
        cur = self._con.cursor()
        rel = cur.sql(f"SELECT {self._select_list(False)} FROM orders_src WHERE id <= {self.rows}")
        pending: list[pa.RecordBatch] = []
        pending_rows = 0
        for batch in _arrow_reader(rel, chunk_rows):
            pending.append(batch)
            pending_rows += batch.num_rows
            if pending_rows >= chunk_rows:
                yield pa.Table.from_batches(pending)
                pending, pending_rows = [], 0
        if pending:
            yield pa.Table.from_batches(pending)
        cur.close()

    def extra_tuples(self) -> list[tuple]:
        """Rows with ids above the base table: the pool that insert workloads draw from."""
        return self._con.execute(
            f"SELECT {self._select_list(True)} FROM orders_src WHERE id > {self.rows} ORDER BY id"
        ).fetchall()

    def customer_tuples(self) -> list[tuple]:
        return self._con.execute("SELECT * FROM customers_src ORDER BY id").fetchall()

    def customer_arrow(self) -> pa.Table:
        rel = self._con.sql("SELECT * FROM customers_src ORDER BY id")
        return pa.Table.from_batches(list(_arrow_reader(rel, 1_000_000)))

    def close(self) -> None:
        self._con.close()


def _arrow_reader(rel, batch_rows: int):
    """Arrow RecordBatchReader for a relation, across DuckDB API generations."""
    for name in ("to_arrow_reader", "fetch_arrow_reader", "fetch_record_batch", "record_batch"):
        method = getattr(rel, name, None)
        if method is not None:
            return method(batch_rows)
    raise RuntimeError("this DuckDB version exposes no Arrow record-batch reader")
