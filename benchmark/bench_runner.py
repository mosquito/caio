#!/usr/bin/env python3
"""
Run each caio backend in a separate subprocess so thread pools from one
backend cannot influence measurements of the next.

Every per-backend CSV uses the same schema. The merge step validates that
schema instead of silently attaching an outdated header to wider rows.

Usage:
  CAIO_BENCH_DATA=/tmp/caio-bench CAIO_RESULTS=/tmp/results uv run python bench_runner.py
"""
import csv
import os
import pathlib
import subprocess
import sys

BACKENDS = ["linux_uring", "linux_aio", "thread_aio", "python_aio"]

RESULTS_DIR = pathlib.Path(os.environ.get("CAIO_RESULTS", "/tmp/results"))
DATA_DIR    = pathlib.Path(os.environ.get("CAIO_BENCH_DATA", "/tmp/caio-bench"))
BENCH       = pathlib.Path(__file__).parent / "bench.py"

env = os.environ.copy()

CSV_COLUMNS = [
    "backend",
    "sweep",
    "op",
    "concurrency",
    "chunk_bytes",
    "latency_us",
    "wall_s",
    "n_ops",
]


def run_backend(name: str):
    print(f"\n{'━' * 80}", flush=True)
    print(f"  backend: {name}", flush=True)
    print(f"{'━' * 80}", flush=True)

    proc = subprocess.run(
        [sys.executable, str(BENCH), "--backend", name],
        env=env,
        cwd=BENCH.parent,
    )

    if proc.returncode != 0:
        print(f"[!] {name} exited with code {proc.returncode}", flush=True)


def merge_results():
    # Merge all per-backend CSVs into one, rejecting incompatible files
    # rather than producing a syntactically valid but column-shifted result.
    rows: list[dict[str, str]] = []

    def collect(path: pathlib.Path):
        if not path.exists():
            return
        with path.open(newline="") as fp:
            reader = csv.DictReader(fp)
            if reader.fieldnames != CSV_COLUMNS:
                print(
                    f"[!] {path}: incompatible CSV header "
                    f"{reader.fieldnames!r}; expected {CSV_COLUMNS!r}",
                    flush=True,
                )
                return
            rows.extend(reader)

    for name in BACKENDS:
        collect(RESULTS_DIR / f"bench_{name}.csv")

    merged = RESULTS_DIR / "bench_all.csv"
    with merged.open("w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nMerged CSV → {merged}")


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    for name in BACKENDS:
        run_backend(name)

    merge_results()


if __name__ == "__main__":
    main()
