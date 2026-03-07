#!/usr/bin/env python3
"""
caio benchmark — latency distribution & throughput.

Two orthogonal sweeps for each backend:

  1. concurrency sweep  — fix chunk=16 KB, vary in-flight ops: 1…512
  2. chunk-size sweep   — fix concurrency=64, vary chunk: 4 KB…1 MB

Metrics per cell: ops/s · MB/s · mean · p50 · p95 · p99 · p999  (ms)

Usage:
  CAIO_BENCH_DATA=/tmp/caio-bench CAIO_RESULTS=/tmp/results uv run python bench.py
"""
import asyncio
import importlib
import itertools
import os
import pathlib
import random
import sys
import tempfile
import time
from typing import Callable, Iterator, List, Tuple

DATA_DIR    = pathlib.Path(os.environ.get("CAIO_BENCH_DATA", "data"))
RESULTS_DIR = pathlib.Path(os.environ.get("CAIO_RESULTS", "/tmp/results"))

TOTAL_OPS    = 10_000
WARMUP_OPS   = 500
MAX_REQUESTS = 4096

# sweep 1 — vary concurrency, fixed chunk
CONC_SWEEP_CHUNK = 16 * 1024
CONC_SWEEP       = [1, 4, 16, 64, 256, 512, 1024, 2048, 4096]

# sweep 2 — vary chunk size, fixed concurrency
CHUNK_SWEEP_CONC   = 64
CHUNK_SWEEP_SIZES  = [4 * 1024, 16 * 1024, 64 * 1024, 256 * 1024, 1024 * 1024, 4 * 1024 * 1024]

BACKENDS = [
    ("linux_uring", "caio.linux_uring_asyncio", "AsyncioContext"),
    ("linux_aio",   "caio.linux_aio_asyncio",   "AsyncioContext"),
    ("thread_aio",  "caio.thread_aio_asyncio",  "AsyncioContext"),
    ("python_aio",  "caio.python_aio_asyncio",  "AsyncioContext"),
]


# ── stats ─────────────────────────────────────────────────────────────────────

def pct(data: List[float], p: float) -> float:
    s = sorted(data)
    return s[min(int(len(s) * p), len(s) - 1)]


def calc_stats(lats: List[float], wall: float, chunk: int, n_total: int) -> dict:
    ops = n_total / wall
    return dict(
        n_ops  = n_total,
        wall_s = wall,
        ops_s  = ops,
        mb_s   = ops * chunk / 1e6,
        mean   = sum(lats) / len(lats) * 1e3,
        p50    = pct(lats, 0.500) * 1e3,
        p95    = pct(lats, 0.950) * 1e3,
        p99    = pct(lats, 0.990) * 1e3,
        p999   = pct(lats, 0.999) * 1e3,
    )


# ── I/O sources ───────────────────────────────────────────────────────────────

class ReadPool:
    def __init__(self):
        self._fps = []
        self._fds: List[Tuple[int, int]] = []

    def open(self):
        paths = sorted(DATA_DIR.glob("*.bin"))
        if not paths:
            sys.exit(f"No .bin files in {DATA_DIR}. Run gen_data.py first.")
        for p in paths:
            fp = open(p, "rb")
            self._fps.append(fp)
            self._fds.append((fp.fileno(), p.stat().st_size))

    def args(self, chunk: int) -> Tuple[int, int, int]:
        fd, size = random.choice(self._fds)
        hi = max(0, size - chunk)
        offset = (random.randint(0, hi) >> 12) << 12   # 4 KB aligned
        return chunk, fd, offset

    def pregenerate(self, n: int, chunk: int) -> Iterator[Tuple[int, int, int]]:
        return itertools.cycle([self.args(chunk) for _ in range(n)])

    def pregenerate_seq(self, chunk: int) -> Iterator[Tuple[int, int, int]]:
        """Sequential read offsets cycling through all open files."""
        args = []
        for fd, size in self._fds:
            offset = 0
            while offset + chunk <= size:
                args.append((chunk, fd, offset))
                offset += chunk
        if not args:
            return self.pregenerate(1, chunk)
        return itertools.cycle(args)

    def close(self):
        for fp in self._fps:
            fp.close()


class WriteTarget:
    # big enough for any chunk × (ops + warmup)
    SIZE = 1024 * 1024 * 1024  # 1 GB preallocated sparse file

    def __init__(self):
        self._fp = tempfile.NamedTemporaryFile(delete=False)
        self._fp.seek(self.SIZE - 1)
        self._fp.write(b"\x00")
        self._fp.flush()
        self.fd  = self._fp.fileno()
        self._path = pathlib.Path(self._fp.name)
        self._bufs: dict[int, bytes] = {}

    def args(self, chunk: int) -> Tuple[bytes, int, int]:
        if chunk not in self._bufs:
            self._bufs[chunk] = os.urandom(chunk)
        hi = self.SIZE - chunk
        offset = (random.randint(0, hi) >> 12) << 12
        return self._bufs[chunk], self.fd, offset

    def pregenerate(self, n: int, chunk: int) -> Iterator[Tuple[bytes, int, int]]:
        return itertools.cycle([self.args(chunk) for _ in range(n)])

    def pregenerate_seq(self, chunk: int) -> Iterator[Tuple[bytes, int, int]]:
        """Sequential write offsets from 0 to SIZE."""
        if chunk not in self._bufs:
            self._bufs[chunk] = os.urandom(chunk)
        n_slots = self.SIZE // chunk
        args = [(self._bufs[chunk], self.fd, i * chunk) for i in range(n_slots)]
        return itertools.cycle(args)

    def close(self):
        self._fp.close()
        self._path.unlink(missing_ok=True)


# ── engine ────────────────────────────────────────────────────────────────────

SAMPLE_RATE = 0.1   # time 10% of ops, rest run without perf_counter overhead


async def run(
    ctx,
    concurrency: int,
    n: int,
    op_fn: Callable,
    warmup: int,
) -> Tuple[List[float], float]:
    """
    Run op_fn(ctx) exactly n times with `concurrency` ops in-flight at most.

    Uses a sliding window: exactly `concurrency` tasks exist at any time,
    a new one is spawned as soon as any completes.  No semaphore needed.

    Only a fraction (SAMPLE_RATE) of ops are individually timed to avoid
    polluting the measurement with clock overhead.  Throughput is always
    derived from wall time.

    Returns (sampled_latencies_in_seconds, wall_clock_seconds).
    """

    async def timed() -> float:
        t = time.perf_counter()
        await op_fn(ctx)
        return time.perf_counter() - t

    async def untimed() -> float:
        await op_fn(ctx)
        return 0.0

    sampled: set = set()      # tasks whose result is a real latency
    _perf_counter = time.perf_counter

    async def _drain(pending, collect=None):
        done, pending = await asyncio.wait(
            pending, return_when=asyncio.FIRST_COMPLETED,
        )
        if collect is not None:
            for task in done:
                if task in sampled:
                    collect.append(task.result())
                    sampled.discard(task)
        else:
            sampled.difference_update(done)
        return pending

    # Warmup — sliding window, discard results
    if warmup:
        pending: set = set()
        left = warmup
        while left > 0 or pending:
            while len(pending) < concurrency and left > 0:
                pending.add(asyncio.create_task(untimed()))
                left -= 1
            if pending:
                pending = await _drain(pending)

    # Decide which ops to sample (pre-generate for zero per-op overhead)
    sample_mask = bytearray(1 if random.random() < SAMPLE_RATE else 0 for _ in range(n))

    # Measurement — sliding window, sampled latencies
    latencies: List[float] = []
    pending = set()
    idx = 0

    t0 = _perf_counter()
    while idx < n or pending:
        while len(pending) < concurrency and idx < n:
            if sample_mask[idx]:
                task = asyncio.create_task(timed())
                sampled.add(task)
            else:
                task = asyncio.create_task(untimed())
            pending.add(task)
            idx += 1
        if pending:
            pending = await _drain(pending, latencies)
    wall = _perf_counter() - t0

    return latencies, wall


# ── formatting ────────────────────────────────────────────────────────────────

def _hdr(pivot_label: str, pivot_width: int) -> str:
    return (
        f"{'backend':<14} {'op':<5} {pivot_label:>{pivot_width}}"
        f"  {'ops/s':>8}  {'MB/s':>6}  {'wall':>6}"
        f"  {'mean':>7}  {'p50':>7}  {'p95':>7}  {'p99':>7}  {'p999':>8}  (ms)"
    )


def _row(backend: str, op: str, pivot: str, pivot_w: int, s: dict) -> str:
    return (
        f"{backend:<14} {op:<5} {pivot:>{pivot_w}}"
        f"  {s['ops_s']:>8.0f}  {s['mb_s']:>6.1f}  {s['wall_s']:>5.2f}s"
        f"  {s['mean']:>7.3f}  {s['p50']:>7.3f}"
        f"  {s['p95']:>7.3f}  {s['p99']:>7.3f}  {s['p999']:>8.3f}"
    )


# ── sweeps ────────────────────────────────────────────────────────────────────

async def sweep_concurrency(
    name: str, CtxCls, pool: ReadPool, write: WriteTarget,
    access: str = "rand",
) -> List[str]:
    """Vary concurrency at fixed chunk size."""
    csv: List[str] = []
    chunk = CONC_SWEEP_CHUNK
    total = TOTAL_OPS + WARMUP_OPS
    sweep_tag = f"conc_sweep_{access}"

    for conc in CONC_SWEEP:
        ctx = CtxCls(max_requests=MAX_REQUESTS)
        pivot = str(conc)
        if access == "seq":
            read_it  = pool.pregenerate_seq(chunk)
            write_it = write.pregenerate_seq(chunk)
        else:
            read_it  = pool.pregenerate(total, chunk)
            write_it = write.pregenerate(total, chunk)

        for op_name, op_fn in [
            ("read",  lambda c: c.read(*next(read_it))),
            ("write", lambda c: c.write(*next(write_it))),
        ]:
            lats, wall = await run(ctx, conc, TOTAL_OPS, op_fn, WARMUP_OPS)
            s = calc_stats(lats, wall, chunk, TOTAL_OPS)
            print(_row(name, op_name, pivot, 5, s), flush=True)
            for lat in lats:
                csv.append(
                    f"{name},{sweep_tag},{op_name},{conc},{chunk},"
                    f"{lat * 1e6:.1f},{s['wall_s']:.4f},{s['n_ops']}"
                )

        ctx.close()

    return csv


async def sweep_chunk(
    name: str, CtxCls, pool: ReadPool, write: WriteTarget,
    access: str = "rand",
) -> List[str]:
    """Vary chunk size at fixed concurrency."""
    csv: List[str] = []
    conc = CHUNK_SWEEP_CONC
    ctx  = CtxCls(max_requests=MAX_REQUESTS)
    total = TOTAL_OPS + WARMUP_OPS
    sweep_tag = f"chunk_sweep_{access}"

    for chunk in CHUNK_SWEEP_SIZES:
        pivot = f"{chunk // 1024}K"
        if access == "seq":
            read_it  = pool.pregenerate_seq(chunk)
            write_it = write.pregenerate_seq(chunk)
        else:
            read_it  = pool.pregenerate(total, chunk)
            write_it = write.pregenerate(total, chunk)

        for op_name, op_fn in [
            ("read",  lambda c: c.read(*next(read_it))),
            ("write", lambda c: c.write(*next(write_it))),
        ]:
            lats, wall = await run(ctx, conc, TOTAL_OPS, op_fn, WARMUP_OPS)
            s = calc_stats(lats, wall, chunk, TOTAL_OPS)
            print(_row(name, op_name, pivot, 5, s), flush=True)
            for lat in lats:
                csv.append(
                    f"{name},{sweep_tag},{op_name},{conc},{chunk},"
                    f"{lat * 1e6:.1f},{s['wall_s']:.4f},{s['n_ops']}"
                )

    ctx.close()
    return csv


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    pool = ReadPool()
    pool.open()
    write = WriteTarget()

    available = []
    for label, mod_name, cls_name in BACKENDS:
        try:
            m = importlib.import_module(mod_name)
            available.append((label, getattr(m, cls_name)))
        except ImportError as e:
            print(f"skip {label}: {e}", file=sys.stderr)

    if not available:
        sys.exit("No backends available.")

    CSV_HEADER = "backend,sweep,op,concurrency,chunk_bytes,latency_us,wall_s,n_ops\n"

    per_backend: dict[str, List[str]] = {}
    for name, Cls in available:
        per_backend.setdefault(name, [])

    def _run_sweep(title: str, hdr: str, coro_fn):
        sep = "─" * len(hdr)
        print(f"\n{'━'*len(hdr)}")
        print(title)
        print(hdr)
        print(sep)
        return sep

    # ── sweep 1: concurrency, random ─────────────────────────────────────────
    hdr1 = _hdr("conc", 5)
    sep1 = _run_sweep(
        f"SWEEP 1 — concurrency / random  (chunk={CONC_SWEEP_CHUNK//1024} KB, ops={TOTAL_OPS})",
        hdr1, None,
    )
    for name, Cls in available:
        rows = await sweep_concurrency(name, Cls, pool, write, access="rand")
        per_backend[name].extend(rows)
        print(sep1)

    # ── sweep 2: chunk size, random ───────────────────────────────────────────
    hdr2 = _hdr("chunk", 5)
    sep2 = _run_sweep(
        f"SWEEP 2 — chunk size / random  (concurrency={CHUNK_SWEEP_CONC}, ops={TOTAL_OPS})",
        hdr2, None,
    )
    for name, Cls in available:
        rows = await sweep_chunk(name, Cls, pool, write, access="rand")
        per_backend[name].extend(rows)
        print(sep2)

    # ── sweep 3: concurrency, sequential ─────────────────────────────────────
    sep3 = _run_sweep(
        f"SWEEP 3 — concurrency / sequential  (chunk={CONC_SWEEP_CHUNK//1024} KB, ops={TOTAL_OPS})",
        hdr1, None,
    )
    for name, Cls in available:
        rows = await sweep_concurrency(name, Cls, pool, write, access="seq")
        per_backend[name].extend(rows)
        print(sep3)

    # ── sweep 4: chunk size, sequential ──────────────────────────────────────
    sep4 = _run_sweep(
        f"SWEEP 4 — chunk size / sequential  (concurrency={CHUNK_SWEEP_CONC}, ops={TOTAL_OPS})",
        hdr2, None,
    )
    for name, Cls in available:
        rows = await sweep_chunk(name, Cls, pool, write, access="seq")
        per_backend[name].extend(rows)
        print(sep4)

    # ── save CSVs — always one file per backend ───────────────────────────────
    for name, rows in per_backend.items():
        out = RESULTS_DIR / f"bench_{name}.csv"
        out.write_text(CSV_HEADER + "\n".join(rows) + "\n")
        print(f"CSV → {out}")

    pool.close()
    write.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backend",
        choices=[b[0] for b in BACKENDS],
        default=None,
        help="Run a single backend (default: all)",
    )
    args = parser.parse_args()

    if args.backend:
        BACKENDS[:] = [b for b in BACKENDS if b[0] == args.backend]

    asyncio.run(main())
