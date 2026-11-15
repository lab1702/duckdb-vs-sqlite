# DuckDB vs SQLite

A benchmark suite that measures **SQLite** against **DuckDB** from Python and turns the
results into a single-file **interactive dashboard**.

It covers the full grid:

| Dimension      | Levels                                                              |
| -------------- | ------------------------------------------------------------------- |
| Workload class | OLTP (point operations) and OLAP (whole-table analytics)            |
| Operation      | Reads and writes                                                    |
| Database size  | Small and large (10 thousand and 1 million rows by default)         |
| Row width      | Narrow (9 columns, ~100 bytes) and wide (21 columns, ~1.3 KB)       |

26 workloads run on every engine for every size × width combination.

## Quick start

Requires Python 3.11 or newer. Works on Linux, Windows and macOS; nothing is compiled
locally (DuckDB, PyArrow and psutil ship wheels for all three).

With [uv](https://docs.astral.sh/uv/):

```bash
uv run dvs run
```

With plain pip - Linux / macOS:

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install . && dvs run
```

With plain pip - Windows (PowerShell):

```powershell
py -m venv .venv; .venv\Scripts\Activate.ps1; pip install .; dvs run
```

The run prints a comparison table per database shape, writes `results/results-<timestamp>.json`,
builds `results/dashboard.html` and opens it in your browser. The dashboard is one
self-contained HTML file with no external requests: open it from disk, e-mail it, or host it.

### Always the latest versions

No dependency is pinned and no lockfile is committed (`uv.lock` is git-ignored), so every
fresh install resolves to the newest release of DuckDB, PyArrow, psutil and Rich. To move
an existing environment forward: `uv sync --upgrade` or
`pip install --upgrade --upgrade-strategy eager .` (plain `--upgrade` leaves dependencies that
are already installed where they are).

SQLite is the one exception you do not control from here: Python's `sqlite3` module uses the
SQLite library bundled with (Windows, macOS python.org builds) or linked by (most Linux builds)
your Python. The dashboard shows the exact versions that were measured.

## Commands

```text
dvs run      run the benchmark, save results, build and open the dashboard
dvs report   rebuild the dashboard from saved results
dvs list     list the workloads and their keys
```

Useful `dvs run` options:

| Option                      | What it does                                                                     |
| --------------------------- | -------------------------------------------------------------------------------- |
| `--preset quick`            | 2K and 50K rows, about half a minute. A sanity check, not a measurement.         |
| `--preset standard`         | Default. 10K and 1M rows, a few minutes, peaks at ~3 GB of scratch disk.         |
| `--preset full`             | 10K, 250K and 5M rows. Long; the wide 5M-row SQLite file is ~7 GB, twice that while it loads. |
| `--rows small=1e4,large=5e6`| Your own database sizes (any number of them).                                    |
| `--widths narrow`           | Test only one row width.                                                         |
| `--duckdb-single-thread`    | Adds "DuckDB (1 thread)" as a third engine: separates parallelism from design.   |
| `--sqlite-synchronous NORMAL` | Relax SQLite durability (default `FULL` = fsync per commit, same as DuckDB).   |
| `--only "point_.*"`        | Record only workloads whose key matches the regex.                               |
| `--workdir D:\bench`        | Where scratch databases live. Put it on the disk you want to measure.            |
| `--repeats`, `--oltp-read-ops`, `--oltp-write-ops`, `--budget` | Sample counts and the per-workload time budget. |
| `--no-open`                 | Do not launch a browser (CI, SSH).                                               |

### Comparing machines or operating systems

Run the benchmark on each machine, collect the JSON files, and build one dashboard with a
run selector:

```bash
dvs report linux.json windows.json macos.json -o compare.html
```

## The dashboard

One filter row (database size, row width, OLTP/OLAP, read/write, statistic) scopes every
view below it:

- **Summary tiles** - geometric-mean speed-up per workload class, plus size on disk.
- **Who is faster, and by how much** - one diverging bar per workload on a log scale.
- **Absolute time per operation** - both engines on a shared log axis, microseconds to seconds.
- **Every database shape at a glance** - a workload × (size, width) heatmap; click a column
  to switch shape, a cell to drill in.
- **Workload drill-down** - how one workload scales with table size and row width, its
  latency distribution (percentile curve or individual runs), full statistics, and the exact SQL.
- **Size on disk** - bytes per row for every shape.
- **All numbers** - sortable table, with a CSV export of every result in the run.
- **How this was measured** - machine, versions, engine settings, caveats.

Hover (or tab to) any mark for exact values; click any workload row to drill into it. Light
and dark themes follow the system setting and can be toggled.

## What is measured

| Class      | Workloads                                                                                                    |
| ---------- | ------------------------------------------------------------------------------------------------------------ |
| OLTP read  | open + first lookup, primary-key lookup, secondary-index lookup, paged range scan, lookup with join          |
| OLTP write | single-row insert / update / delete (one durable commit each), read-modify-write transaction, `executemany` batch |
| OLAP read  | full and filtered aggregates, group-by (few / many groups), join + aggregate, monthly time series, top-N, count distinct, window function, substring search, 5,000-row export |
| OLAP write | bulk load, create index, create-table-as-select, bulk update, bulk delete                                    |

The schema is an `orders` fact table with a primary key and one secondary index, plus a small
`customers` dimension. Data is synthetic and deterministic (fixed seed).

### Method, and how the comparison is kept fair

- **Identical data and queries.** Rows are generated once per shape and streamed to both
  engines; parameters come from a seeded generator that does not depend on the engine. SQL is
  the same except where the dialects differ (date bucketing), and the dashboard shows both.
  Generating data is never inside a timed section.
- **Timed from the application's point of view.** Each sample is `execute()` plus `fetchall()`,
  measured with `time.perf_counter_ns()`. Driver overhead is deliberately included.
- **Equal durability.** DuckDB fsyncs its WAL on commit; SQLite runs in WAL mode with
  `synchronous=FULL` to match. SQLite also gets a 256 MB page cache instead of its 2 MB default.
- **Default parallelism.** DuckDB uses all cores (its default); SQLite is single-threaded per
  query. Use `--duckdb-single-thread` to see how much of a gap is parallelism.
- **Idiomatic bulk load, plus an apples-to-apples one.** "Bulk load" uses each engine's fast
  path (SQLite: `executemany` of tuples; DuckDB: `INSERT ... SELECT` from Arrow). "Batch insert"
  uses `executemany` on both.
- **Order of operations.** Load, index, checkpoint, then all reads, then single-row writes,
  then the statements that rewrite 5% of the table - so reads never see a mutated table.
  Those last statements run on a table the single-row writes have grown; the dashboard's
  scaling factors use the size it has by then.
- **Samples.** Analytical queries get one warm-up and up to `--repeats` timed runs; OLTP
  workloads get hundreds to thousands of operations. A soft time budget trims sample counts for
  very slow workloads; the dashboard reports `n` everywhere.

### What it does not measure

Concurrent connections or writers, cold operating-system caches, network access, in-memory
databases, or other drivers (APSW, the DuckDB CLI, other languages). Results describe one
machine and one configuration - rerun it on the hardware you care about.

## Development

```bash
uv run pytest
```

The smoke test runs every workload on every engine variant against a tiny dataset, checks that
both engines return the same row counts, and builds a dashboard. `.github/workflows/ci.yml`
runs it, plus a `--preset quick` benchmark, on Ubuntu, Windows and macOS.

```text
src/duckdb_vs_sqlite/
  config.py       sizes, widths, engine variants, presets
  datagen.py      deterministic synthetic data
  engines.py      connection setup, DDL and bulk-load path per engine
  workloads.py    the 26 workloads
  runner.py       orchestration, timing budgets, results JSON
  stats.py        summary statistics
  sysinfo.py      machine and version capture
  report.py       embeds results into the dashboard template
  dashboard.html  the dashboard (vanilla HTML/CSS/JS, no dependencies)
  cli.py          the `dvs` command
```
