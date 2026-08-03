#!/usr/bin/env python3
"""Fails if any Rust crate's `unsafe` surface grew past its tracked baseline.

Per design/generalized-safe-design.md's testing strategy ("CI check,
запрещающий расширение unsafe allowlist"): `caio-core` already enforces zero
unsafe at compile time (`#![deny(unsafe_code)]`), but the driver/bridge
crates genuinely need some (raw syscalls, mmap, PyO3 FFI) - the goal here
isn't zero, it's making any *growth* in that surface a deliberate, reviewed
change instead of something that can silently creep in one PR at a time.

Counts `unsafe fn`/`unsafe impl`/`unsafe trait`/`unsafe {` occurrences per
crate (unsafe *constructs*, not the word "unsafe" anywhere - many doc
comments and SAFETY: notes use the word in prose without introducing any).
Compares against .github/unsafe-baseline.txt. A count higher than its
baseline fails the check; equal or lower passes (lowering the actual count
doesn't require touching the baseline - only growth needs a deliberate
bump here).
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / ".github" / "unsafe-baseline.txt"
UNSAFE_RE = re.compile(r"\bunsafe\s*(\{|fn\b|impl\b|trait\b)")


def count_unsafe(crate_dir: Path) -> int:
    total = 0
    for rs_file in crate_dir.rglob("*.rs"):
        total += len(UNSAFE_RE.findall(rs_file.read_text(encoding="utf-8")))
    return total


def load_baseline() -> dict[str, int]:
    baseline = {}
    for line in BASELINE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        crate, count = line.rsplit(maxsplit=1)
        baseline[crate] = int(count)
    return baseline


def main() -> int:
    baseline = load_baseline()
    failures = []
    report_lines = []

    for crate, allowed in sorted(baseline.items()):
        crate_dir = REPO_ROOT / crate
        if not crate_dir.is_dir():
            failures.append(f"{crate}: listed in baseline but directory does not exist")
            continue
        actual = count_unsafe(crate_dir)
        status = "OK" if actual <= allowed else "GREW"
        report_lines.append(f"  {crate}: {actual} (baseline {allowed}) [{status}]")
        if actual > allowed:
            failures.append(
                f"{crate}: unsafe count grew from {allowed} to {actual} - "
                f"if this growth is deliberate and reviewed, update {BASELINE_PATH.relative_to(REPO_ROOT)} "
                f"in the same PR; if not, remove the new unsafe usage",
            )

    print("Unsafe construct counts (unsafe fn/impl/trait/block):")
    print("\n".join(report_lines))

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nOK: no crate's unsafe surface grew past its tracked baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
