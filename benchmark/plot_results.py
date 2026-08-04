"""
Plot and compare caio benchmark results with and without the GIL.

Produces figures saved to CAIO_RESULTS dir (once per access pattern: rand/seq):
  concurrency_sweep_throughput_{rand,seq}.png
  concurrency_sweep_latency_{rand,seq}.png
  chunk_sweep_throughput_{rand,seq}.png
  chunk_sweep_latency_{rand,seq}.png
  latency_histograms.png  (rand only)

Usage:
  CAIO_RESULTS_GIL=/tmp/results-gil \
  CAIO_RESULTS_NOGIL=/tmp/results-nogil \
  PLOT_RESULTS=/tmp/results \
    uv run --with matplotlib --with numpy python plot_results.py
"""
import csv
import gzip
import os
import pathlib
from collections import defaultdict
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import ticker
from PIL import Image

SCRIPT_DIR = pathlib.Path(__file__).parent
RESULTS_DIR = SCRIPT_DIR / "results"
PLOT_RESULTS_DIR = pathlib.Path(os.environ.get("PLOT_RESULTS", RESULTS_DIR))
CSV_GIL_PATH = (
    pathlib.Path(os.environ["CAIO_RESULTS_GIL"]) / "bench_all.csv"
    if "CAIO_RESULTS_GIL" in os.environ
    else RESULTS_DIR / "bench_all.csv.gz"
)
CSV_NOGIL_PATH = (
    pathlib.Path(os.environ["CAIO_RESULTS_NOGIL"]) / "bench_all.csv"
    if "CAIO_RESULTS_NOGIL" in os.environ
    else RESULTS_DIR / "bench_all_nogil.csv.gz"
)

OUTPUT_SUFFIX = ""
MODE_LABEL = ""

BACKENDS = [
    "linux_uring",
    "linux_aio",
    "thread_aio",
    "python_aio",
]

OP_COLORS  = {"read": "#3498db", "write": "#e74c3c"}
OP_MARKERS = {"read": "o", "write": "s"}



# ── data loading ──────────────────────────────────────────────────────────────

Row = dict[str, str]

def load_csv(path: pathlib.Path) -> list[Row]:
    if path.suffix == ".gz":
        with gzip.open(path, mode="rt", newline="") as fp:
            return list(csv.DictReader(fp))
    with path.open(mode="r", newline="") as fp:
        return list(csv.DictReader(fp))


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(int(len(s) * p), len(s) - 1)]


CellData = dict[str, Any]  # keys: lats (List[float]), wall_s (float), n_ops (int)


def group(
    rows: list[Row],
    sweep: str,
    op: str,
) -> defaultdict[str, defaultdict[int, CellData]]:
    """Returns {backend: {pivot_value: {lats: [...], wall_s: float, n_ops: int}}}.

    `sweep` is the full sweep tag, e.g. 'conc_sweep_rand' or 'chunk_sweep_seq'.
    The pivot key is concurrency for conc sweeps, chunk_bytes for chunk sweeps.
    """
    is_conc = sweep.startswith("conc_sweep")
    result: defaultdict[str, defaultdict[int, CellData]] = defaultdict(
        lambda: defaultdict(lambda: {"lats": [], "wall_s": 0.0, "n_ops": 0}),
    )
    for r in rows:
        if r["sweep"] != sweep or r["op"] != op:
            continue
        if not r.get("latency_us"):
            continue
        pivot = int(r["concurrency"] if is_conc else r["chunk_bytes"])
        cell = result[r["backend"]][pivot]
        cell["lats"].append(float(r["latency_us"]))
        if r.get("wall_s"):
            cell["wall_s"] = float(r["wall_s"])
        if r.get("n_ops"):
            cell["n_ops"] = int(r["n_ops"])
    return result


# ── plot helpers ──────────────────────────────────────────────────────────────

def apply_style(ax, xlabel: str, ylabel: str, title: str,
                xscale="linear", yscale="linear"):
    ax.set_xlabel(xlabel, fontsize=10)
    ax.set_ylabel(ylabel, fontsize=10)
    ax.set_title(title, fontsize=11, fontweight="bold")
    ax.set_xscale(xscale)
    ax.set_yscale(yscale)
    ax.grid(True, which="both", linestyle="--", alpha=0.4)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def _mode_title(title: str) -> str:
    if not MODE_LABEL:
        return title
    return f"{title} — {MODE_LABEL}"


def _output_path(name: str) -> pathlib.Path:
    stem, suffix = os.path.splitext(name)
    return PLOT_RESULTS_DIR / f"{stem}{OUTPUT_SUFFIX}{suffix}"


def _percentile_footer(
    ax,
    rows: list[Row],
    sweep: str,
    backend: str,
) -> None:
    """Compact aggregate latency summary below every subplot."""
    lines = []
    for op in ("read", "write"):
        values = [
            float(row["latency_us"]) / 1000
            for row in rows
            if row["sweep"] == sweep
            and row["backend"] == backend
            and row["op"] == op
            and row.get("latency_us")
        ]
        if not values:
            continue
        stats = "  ".join(
            f"p{int(quantile * 100)} {pct(values, quantile):.3f}"
            for quantile in (0.25, 0.50, 0.95, 0.99)
        )
        lines.append(f"{op}: {stats} ms")

    if lines:
        ax.text(
            0.5,
            -0.30,
            "\n".join(lines),
            transform=ax.transAxes,
            ha="center",
            va="top",
            fontsize=6.2,
            color="#555555",
            linespacing=1.35,
        )


def _combine_mode_rows(
    gil_path: pathlib.Path,
    nogil_path: pathlib.Path,
    output_path: pathlib.Path,
) -> None:
    """Stack GIL above no-GIL while preserving each row's exact layout."""
    with Image.open(gil_path) as gil_image, Image.open(nogil_path) as nogil_image:
        width = max(gil_image.width, nogil_image.width)
        height = gil_image.height + nogil_image.height
        combined = Image.new("RGB", (width, height), "white")
        combined.paste(gil_image.convert("RGB"), (0, 0))
        combined.paste(nogil_image.convert("RGB"), (0, gil_image.height))
        combined.save(output_path)


def fmt_chunk(b: int) -> str:
    if b >= 1024 * 1024:
        return f"{b // (1024*1024)}M"
    return f"{b // 1024}K"


def _op_legend(ax):
    """Legend for read/write ops + band explanation."""
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    elems = [
        Line2D([0], [0], color=OP_COLORS[op], linewidth=2, marker=OP_MARKERS[op],
               markersize=5, label=op)
        for op in ("read", "write")
    ] + [
        Patch(facecolor="gray", alpha=0.25, label="p25–p75"),
        Patch(facecolor="gray", alpha=0.10, label="p5–p95"),
    ]
    ax.legend(handles=elems, fontsize=7.5, framealpha=0.7)


def _chunk_xticks(ax, chunks: list[int]):
    """Set numeric x-positions with human-readable tick labels."""
    ax.set_xticks(range(len(chunks)))
    ax.set_xticklabels([fmt_chunk(c) for c in chunks])


def _conc_xticks(ax, values: list[int]):
    """Keep logarithmic concurrency labels readable in wide backend grids."""
    ticks = values
    if len(ticks) > 6:
        ticks = ticks[::2]
        if ticks[-1] != values[-1]:
            ticks.append(values[-1])
    ax.set_xticks(ticks)
    ax.tick_params(axis="x", labelsize=8)


def _available(rows: list[Row]) -> list[str]:
    """Backends that actually appear in the CSV data."""
    present = {r["backend"] for r in rows}
    return [b for b in BACKENDS if b in present]


# ── figure 1: concurrency sweep — throughput ──────────────────────────────────

def plot_conc_throughput(rows: list[Row], access: str = "rand"):
    backends = _available(rows)
    n = len(backends)
    if not n:
        return
    fig, axes = plt.subplots(1, n, figsize=(4.5 * n, 5), sharey=True)
    fig.suptitle(
        _mode_title(
            f"Throughput vs Concurrency  (chunk=16 KB, {access})",
        ),
        fontsize=13,
        fontweight="bold",
    )
    sweep = f"conc_sweep_{access}"
    if n == 1:
        axes = [axes]

    for col, backend in enumerate(backends):
        ax = axes[col]
        xs_all: list[int] = []

        for op in ("read", "write"):
            data = group(rows, sweep, op)
            if backend not in data:
                continue
            d = data[backend]
            xs = sorted(d)
            xs_all = xs
            ys = []
            for x in xs:
                cell = d[x]
                if cell["wall_s"] > 0 and cell["n_ops"] > 0:
                    ys.append(cell["n_ops"] / cell["wall_s"] / 1000)
                else:
                    mean_s = np.mean(cell["lats"]) / 1e6
                    ys.append(x / mean_s / 1000 if mean_s > 0 else 0)

            ax.plot(xs, ys, marker=OP_MARKERS[op], color=OP_COLORS[op],
                    label=op, linewidth=1.8, markersize=6)

        apply_style(ax, "Concurrency", "kops/s", backend, xscale="log")
        ax.xaxis.set_major_formatter(ticker.ScalarFormatter())
        _conc_xticks(ax, xs_all)
        ax.legend(fontsize=8, framealpha=0.7)
        _percentile_footer(ax, rows, sweep, backend)

    fig.tight_layout(rect=(0, 0.07, 1, 0.95), h_pad=3)
    out = _output_path(f"concurrency_sweep_throughput_{access}.png")
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"saved {out}")
    plt.close(fig)


# ── figure 2: concurrency sweep — latency ────────────────────────────────────

def plot_conc_latency(rows: list[Row], access: str = "rand"):
    backends = _available(rows)
    n = len(backends)
    if not n:
        return
    fig, axes = plt.subplots(1, n, figsize=(4.5 * n, 5), sharey=True)
    fig.suptitle(
        _mode_title(f"Latency vs Concurrency  (chunk=16 KB, {access})"),
        fontsize=13,
        fontweight="bold",
    )
    sweep = f"conc_sweep_{access}"
    if n == 1:
        axes = [axes]

    for col, backend in enumerate(backends):
        ax = axes[col]
        pivots: list[int] = []

        for op in ("read", "write"):
            data = group(rows, sweep, op)
            if backend not in data:
                continue
            d = data[backend]
            pivots = sorted(d)
            lats_list = [d[p]["lats"] for p in pivots]
            xs = list(pivots)

            p5s  = [pct(l, 0.05) / 1000 for l in lats_list]
            p25s = [pct(l, 0.25) / 1000 for l in lats_list]
            p50s = [pct(l, 0.50) / 1000 for l in lats_list]
            p75s = [pct(l, 0.75) / 1000 for l in lats_list]
            p95s = [pct(l, 0.95) / 1000 for l in lats_list]

            color = OP_COLORS[op]
            ax.fill_between(xs, p5s, p95s, color=color, alpha=0.10)
            ax.fill_between(xs, p25s, p75s, color=color, alpha=0.25)
            ax.plot(xs, p50s, marker=OP_MARKERS[op], color=color,
                    label=op, linewidth=1.8, markersize=5)

        apply_style(ax, "Concurrency", "Latency (ms)", backend,
                    xscale="log", yscale="log")
        ax.xaxis.set_major_formatter(ticker.ScalarFormatter())
        if pivots:
            _conc_xticks(ax, pivots)
        _op_legend(ax)
        _percentile_footer(ax, rows, sweep, backend)

    fig.tight_layout(rect=(0, 0.07, 1, 0.95), h_pad=3)
    out = _output_path(f"concurrency_sweep_latency_{access}.png")
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"saved {out}")
    plt.close(fig)


# ── figure 3: chunk sweep — throughput (MB/s) ─────────────────────────────────

def plot_chunk_throughput(rows: list[Row], access: str = "rand"):
    backends = _available(rows)
    n = len(backends)
    if not n:
        return
    fig, axes = plt.subplots(1, n, figsize=(4.5 * n, 5), sharey=True)
    fig.suptitle(
        _mode_title(
            f"Throughput vs Chunk Size  (concurrency=64, {access})",
        ),
        fontsize=13,
        fontweight="bold",
    )
    sweep = f"chunk_sweep_{access}"
    if n == 1:
        axes = [axes]

    for col, backend in enumerate(backends):
        ax = axes[col]
        chunks: list[int] = []

        for op in ("read", "write"):
            data = group(rows, sweep, op)
            if backend not in data:
                continue
            d = data[backend]
            chunks = sorted(d)
            xpos = list(range(len(chunks)))
            ys = []
            for chunk in chunks:
                cell = d[chunk]
                if cell["wall_s"] > 0 and cell["n_ops"] > 0:
                    ops_s = cell["n_ops"] / cell["wall_s"]
                else:
                    mean_s = np.mean(cell["lats"]) / 1e6
                    ops_s = 64 / mean_s if mean_s > 0 else 0
                ys.append(ops_s * chunk / 1e6)  # MB/s

            ax.plot(xpos, ys, marker=OP_MARKERS[op], color=OP_COLORS[op],
                    label=op, linewidth=1.8, markersize=6)

        apply_style(ax, "Chunk size", "MB/s", backend)
        _chunk_xticks(ax, chunks)
        ax.legend(fontsize=8, framealpha=0.7)
        _percentile_footer(ax, rows, sweep, backend)

    fig.tight_layout(rect=(0, 0.07, 1, 0.95), h_pad=3)
    out = _output_path(f"chunk_sweep_throughput_{access}.png")
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"saved {out}")
    plt.close(fig)


# ── figure 4: chunk sweep — latency ──────────────────────────────────────────

def plot_chunk_latency(rows: list[Row], access: str = "rand"):
    backends = _available(rows)
    n = len(backends)
    if not n:
        return
    fig, axes = plt.subplots(1, n, figsize=(4.5 * n, 5), sharey=True)
    fig.suptitle(
        _mode_title(f"Latency vs Chunk Size  (concurrency=64, {access})"),
        fontsize=13,
        fontweight="bold",
    )
    sweep = f"chunk_sweep_{access}"
    if n == 1:
        axes = [axes]

    for col, backend in enumerate(backends):
        ax = axes[col]
        chunks: list[int] = []

        for op in ("read", "write"):
            data = group(rows, sweep, op)
            if backend not in data:
                continue
            d = data[backend]
            chunks = sorted(d)
            xpos = list(range(len(chunks)))
            lats_list = [d[c]["lats"] for c in chunks]

            p5s  = [pct(l, 0.05) / 1000 for l in lats_list]
            p25s = [pct(l, 0.25) / 1000 for l in lats_list]
            p50s = [pct(l, 0.50) / 1000 for l in lats_list]
            p75s = [pct(l, 0.75) / 1000 for l in lats_list]
            p95s = [pct(l, 0.95) / 1000 for l in lats_list]

            color = OP_COLORS[op]
            ax.fill_between(xpos, p5s, p95s, color=color, alpha=0.10)
            ax.fill_between(xpos, p25s, p75s, color=color, alpha=0.25)
            ax.plot(xpos, p50s, marker=OP_MARKERS[op], color=color,
                    label=op, linewidth=1.8, markersize=5)

        apply_style(ax, "Chunk size", "Latency (ms)", backend, yscale="log")
        _chunk_xticks(ax, chunks)
        _op_legend(ax)
        _percentile_footer(ax, rows, sweep, backend)

    fig.tight_layout(rect=(0, 0.07, 1, 0.95), h_pad=3)
    out = _output_path(f"chunk_sweep_latency_{access}.png")
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"saved {out}")
    plt.close(fig)


# ── figure 5: latency distribution histograms (concurrency=64, chunk=16K) ────

_HIST_COLORS = ["#3498db", "#e74c3c", "#2ecc71", "#f39c12", "#9b59b6"]


def plot_histograms(rows: list[Row]):
    """Per-backend histogram of read latency at concurrency=64, chunk=16K."""
    backends = _available(rows)
    if not backends:
        return
    target_conc = 64

    ncols = min(len(backends), 4)
    nrows = (len(backends) + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(6.5 * ncols, 4.5 * nrows),
                             squeeze=False)
    fig.suptitle(
        _mode_title(
            f"Read latency distribution  "
            f"(concurrency={target_conc}, chunk=16 KB)",
        ),
        fontsize=13, fontweight="bold",
    )

    for idx, backend in enumerate(backends):
        ax = axes[idx // ncols][idx % ncols]
        lats = [
            float(r["latency_us"]) / 1000   # us -> ms
            for r in rows
            if r["sweep"] == "conc_sweep_rand"
            and r["op"] == "read"
            and r["backend"] == backend
            and r.get("latency_us")
            and int(r["concurrency"]) == target_conc
        ]
        if not lats:
            ax.set_visible(False)
            continue

        p25 = pct(lats, 0.25)
        p50 = pct(lats, 0.50)
        p95 = pct(lats, 0.95)
        p99 = pct(lats, 0.99)
        clip = pct(lats, 0.999)

        ax.hist(
            [min(x, clip) for x in lats],
            bins=80,
            color=_HIST_COLORS[idx % len(_HIST_COLORS)],
            alpha=0.75,
            edgecolor="none",
        )
        for val, label, ls in [(p50, "p50", "-"), (p95, "p95", "--"), (p99, "p99", ":")]:
            ax.axvline(val, color="black", linestyle=ls, linewidth=1.2,
                       label=f"{label}={val:.3f} ms")

        ax.set_title(backend, fontweight="bold")
        ax.set_xlabel("Latency (ms)")
        ax.set_ylabel("Count")
        ax.legend(fontsize=8)
        ax.grid(True, linestyle="--", alpha=0.4)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.text(
            0.5,
            -0.24,
            (
                f"read: p25 {p25:.3f}  p50 {p50:.3f}  "
                f"p95 {p95:.3f}  p99 {p99:.3f} ms"
            ),
            transform=ax.transAxes,
            ha="center",
            va="top",
            fontsize=6.5,
            color="#555555",
        )

    # Hide unused subplot cells
    for idx in range(len(backends), nrows * ncols):
        axes[idx // ncols][idx % ncols].set_visible(False)

    fig.tight_layout(rect=(0, 0.07, 1, 0.95), h_pad=3)
    out = _output_path("latency_histograms.png")
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"saved {out}")
    plt.close(fig)


# ── main ──────────────────────────────────────────────────────────────────────

def _render_mode(rows: list[Row], label: str, suffix: str) -> None:
    global MODE_LABEL, OUTPUT_SUFFIX
    MODE_LABEL = label
    OUTPUT_SUFFIX = suffix
    for access in ("rand", "seq"):
        plot_conc_throughput(rows, access)
        plot_conc_latency(rows, access)
        plot_chunk_throughput(rows, access)
        plot_chunk_latency(rows, access)
    plot_histograms(rows)


def main():
    missing = [
        path for path in (CSV_GIL_PATH, CSV_NOGIL_PATH)
        if not path.exists()
    ]
    if missing:
        paths = "\n".join(str(path) for path in missing)
        raise SystemExit(f"Benchmark CSV not found:\n{paths}")

    PLOT_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    gil_rows = load_csv(CSV_GIL_PATH)
    nogil_rows = load_csv(CSV_NOGIL_PATH)
    print(f"loaded {len(gil_rows):,} GIL rows from {CSV_GIL_PATH}")
    print(f"loaded {len(nogil_rows):,} no-GIL rows from {CSV_NOGIL_PATH}")

    _render_mode(gil_rows, "GIL", "_gil")
    _render_mode(nogil_rows, "free-threaded / no GIL", "_nogil")

    names = [
        f"{prefix}_{access}.png"
        for access in ("rand", "seq")
        for prefix in (
            "concurrency_sweep_throughput",
            "concurrency_sweep_latency",
            "chunk_sweep_throughput",
            "chunk_sweep_latency",
        )
    ] + ["latency_histograms.png"]

    for name in names:
        stem, suffix = os.path.splitext(name)
        gil_path = PLOT_RESULTS_DIR / f"{stem}_gil{suffix}"
        nogil_path = PLOT_RESULTS_DIR / f"{stem}_nogil{suffix}"
        output_path = PLOT_RESULTS_DIR / name
        _combine_mode_rows(gil_path, nogil_path, output_path)
        gil_path.unlink()
        nogil_path.unlink()
        print(f"combined {output_path}")

    print(f"\nAll plots saved to {PLOT_RESULTS_DIR.resolve()}")


if __name__ == "__main__":
    main()
