#!/usr/bin/env python3
"""
Run each caio backend in a separate subprocess so thread pools from one
backend cannot influence measurements of the next.
Also runs the Go goroutine baseline if `go` is available.

Usage:
  CAIO_BENCH_DATA=/tmp/caio-bench CAIO_RESULTS=/tmp/results uv run python bench_runner.py
"""
import os
import pathlib
import shutil
import subprocess
import sys

BACKENDS = ["linux_uring", "linux_aio", "thread_aio", "python_aio"]

RESULTS_DIR = pathlib.Path(os.environ.get("CAIO_RESULTS", "/tmp/results"))
DATA_DIR    = pathlib.Path(os.environ.get("CAIO_BENCH_DATA", "/tmp/caio-bench"))
BENCH       = pathlib.Path(__file__).parent / "bench.py"
BENCH_GO    = pathlib.Path(__file__).parent / "bench_go.go"

env = os.environ.copy()


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


def run_go():
    go_bin = shutil.which("go")
    if not go_bin:
        print("\n[!] go not found in PATH, skipping go_goroutine baseline", flush=True)
        return

    print(f"\n{'━' * 80}", flush=True)
    print(f"  backend: go_goroutine", flush=True)
    print(f"{'━' * 80}", flush=True)

    proc = subprocess.run(
        [go_bin, "run", str(BENCH_GO),
         "-data", str(DATA_DIR),
         "-results", str(RESULTS_DIR)],
        cwd=BENCH_GO.parent,
    )
    if proc.returncode != 0:
        print(f"[!] go_goroutine exited with code {proc.returncode}", flush=True)


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    for name in BACKENDS:
        run_backend(name)

    run_go()

    # merge all per-backend CSVs into one
    header = "backend,sweep,op,concurrency,chunk_bytes,latency_us\n"
    rows: list[str] = []
    for name in BACKENDS:
        f = RESULTS_DIR / f"bench_{name}.csv"
        if not f.exists():
            continue
        lines = f.read_text().splitlines()
        rows.extend(lines[1:])   # skip header

    go_csv = RESULTS_DIR / "bench_go.csv"
    if go_csv.exists():
        lines = go_csv.read_text().splitlines()
        rows.extend(lines[1:])   # skip header

    merged = RESULTS_DIR / "bench_all.csv"
    merged.write_text(header + "\n".join(rows) + "\n")
    print(f"\nMerged CSV → {merged}")


if __name__ == "__main__":
    main()
