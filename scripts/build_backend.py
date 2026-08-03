"""PEP 517 build backend for caio: a thin wrapper around maturin's own
build_sdist/build_wheel/build_editable (see the installed `maturin`
package's own `__init__.py` - a short, subprocess-shelling module we call
into directly), extended to also build the Linux-only linux_aio/
linux_uring sibling crates - these aren't a real Cargo dependency of
thread_aio, the one crate maturin itself manages (maturin only ever
builds one crate per wheel), so maturin's own build never touches them.

This replaces the old mechanism, where `src/thread_aio/build.rs` built
the siblings as a side effect of thread_aio's own `cargo build` (nested
cargo invocation from within a build script, with an OS-temp-dir staging
step to dodge `cargo package`'s sdist workspace rewrite, and a dedicated
`target/siblings` dir to dodge the nested-build lock hazard) - all of
that orchestration now lives here instead, in Python, as the single place
that knows about all three `.abi3.so` files.

Must run under Python 3.10 (every CI/Makefile codepath that actually
builds a wheel, as opposed to running tests, invokes exactly this
interpreter version) - stdlib plus `maturin` only, no other dependency.
"""
import base64
import hashlib
import platform
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import maturin

# Hooks with no sibling-crate involvement at all - delegate straight
# through to maturin's own implementation, unchanged behavior. In
# particular `build_sdist` is untouched: `[tool.maturin] include`'s
# sdist-format globs already include the siblings' own source (and their
# path-dependencies, and this very file - see pyproject.toml), so the
# sdist maturin produces for thread_aio already carries everything a
# later from-sdist build_wheel call here will need.
build_sdist = maturin.build_sdist
get_requires_for_build_sdist = maturin.get_requires_for_build_sdist
get_requires_for_build_wheel = maturin.get_requires_for_build_wheel
get_requires_for_build_editable = maturin.get_requires_for_build_wheel
prepare_metadata_for_build_wheel = maturin.prepare_metadata_for_build_wheel
prepare_metadata_for_build_editable = maturin.prepare_metadata_for_build_wheel

REPO_ROOT = Path(__file__).resolve().parent.parent
CAIO_DIR = REPO_ROOT / "caio"
ABI3_EXT_SUFFIX = ".abi3.so"

# Ported 1:1 from the old build.rs's SIBLINGS/sibling_extra_deps() -
# update here (only) the first time a fourth backend crate is added.
SIBLINGS = ("linux_aio", "linux_uring")
SIBLING_EXTRA_DEPS = {
    "linux_aio": ("caio-core", "caio-backend-linux-aio"),
    "linux_uring": ("caio-core", "caio-backend-uring"),
}


def stage_sibling_source(staging_root: Path, name: str) -> Path:
    """Copies `src/<name>`'s own top-level files (`Cargo.toml` plus its
    `*.rs` files - no subdirectories, e.g. `bin/stub_gen.rs` is
    deliberately NOT staged, a `cargo build --lib` doesn't need it) into
    `staging_root/<name>`, which has no ancestor `Cargo.toml` above it.

    This is required, not just defensive: when maturin packages an sdist
    for thread_aio, `cargo package` rewrites the sdist's top-level
    `Cargo.toml` to a workspace listing only `src/thread_aio`, so a
    from-sdist build's ancestor workspace no longer claims `src/<name>`
    (or its own path-dependencies) as members, and a plain `cargo build`
    against the in-place `src/<name>/Cargo.toml` fails ("current package
    believes it's in a workspace when it's not"). Building a copy with no
    ancestor Cargo.toml above it at all sidesteps this - and works
    identically whether or not this build actually originated from an
    sdist, so it's always done rather than only conditionally.
    """
    source_dir = REPO_ROOT / "src" / name
    staging_dir = staging_root / name
    staging_dir.mkdir(parents=True, exist_ok=True)
    for entry in source_dir.iterdir():
        if entry.is_file():
            shutil.copy2(entry, staging_dir / entry.name)
    return staging_dir


def build_sibling(staging_root: Path, target_dir: Path, name: str) -> Path:
    staging_dir = stage_sibling_source(staging_root, name)
    for extra in SIBLING_EXTRA_DEPS.get(name, ()):
        stage_sibling_source(staging_root, extra)

    cargo = shutil.which("cargo") or "cargo"
    subprocess.run(
        [
            cargo, "build", "--lib", "--release",
            "--manifest-path", str(staging_dir / "Cargo.toml"),
            "--target-dir", str(target_dir),
        ],
        check=True,
    )

    built = target_dir / "release" / f"lib{name}.so"
    if not built.is_file():
        raise RuntimeError(f"expected sibling build output missing: {built}")
    return built


def build_siblings(dest_dir: Path):
    """Builds linux_aio/linux_uring (Linux only - a no-op everywhere
    else, matching build.rs's own `target_os != "linux"` early return)
    and copies the resulting `.abi3.so` into `dest_dir`. Returns a list
    of `(name, path_in_dest_dir)` pairs, empty when skipped.
    """
    if platform.system() != "Linux":
        return []

    # Persistent target-dir (mirrors build.rs's own `target/siblings`):
    # gives cargo's incremental-build cache across repeated dev-loop
    # invocations (`pip install -e .` / `uv sync` run again and again
    # while iterating) - already gitignored and already wiped by `make
    # clean`, so no new cleanup burden. The staging root, in contrast,
    # must always be freshly made to exactly mirror current source state
    # - never reused across calls.
    target_dir = REPO_ROOT / "target" / "siblings"
    results = []
    with tempfile.TemporaryDirectory(prefix="caio-sibling-build-") as tmp:
        staging_root = Path(tmp)
        for name in SIBLINGS:
            built = build_sibling(staging_root, target_dir, name)
            dest = dest_dir / f"{name}{ABI3_EXT_SUFFIX}"
            # copy2 (not copy) preserves the compiled file's real
            # permission bits, so the executable bit rustc/cargo set on
            # its own cdylib output carries through unchanged - no
            # hardcoded chmod here or in the zip-injection step below.
            shutil.copy2(built, dest)
            results.append((name, dest))
    return results


def inject_siblings_into_wheel(wheel_path: Path, siblings) -> None:
    """Rewrites `wheel_path` in place, adding one `caio/<name>.abi3.so`
    entry per sibling and patching `*.dist-info/RECORD` to match.

    Streams every existing entry from the original zip into a brand-new
    zip, rather than extracting to a directory and re-zipping - the only
    way to preserve each pre-existing entry's exact `ZipInfo` (mtime,
    permission bits, compress_type) with zero risk of `zipfile`
    re-deriving any of that differently from a round-tripped-through-the-
    filesystem copy. Written to a sibling temp file first, then
    `os.replace()`d over the original - atomic on POSIX, so a failure
    partway through can never leave a half-written wheel at the final
    path.
    """
    tmp_path = wheel_path.with_suffix(wheel_path.suffix + ".tmp")
    try:
        with zipfile.ZipFile(wheel_path, "r") as src:
            record_name = next(
                n for n in src.namelist() if n.endswith(".dist-info/RECORD")
            )
            record_info = next(
                i for i in src.infolist() if i.filename == record_name
            )
            # Reuse whatever compress_type the primary wheel's own binary
            # entry (thread_aio's own .so) uses, for consistency, instead
            # of hardcoding one.
            so_compress_type = next(
                (i.compress_type for i in src.infolist()
                 if i.filename.endswith(".so")),
                zipfile.ZIP_DEFLATED,
            )

            # maturin's own thread_aio-scoped wheel build walks the whole
            # `python-source = "."` tree (caio/) as pure-python content -
            # if a sibling .so happens to already be sitting there (e.g.
            # a leftover from an earlier `pip install -e .`/`uv sync` in
            # the same checkout, never cleaned before building a wheel),
            # maturin bundles that stale copy into `wheel_path` on its
            # own, before this function ever runs. Drop any such
            # pre-existing entry (both the zip member and its RECORD
            # line) so the freshly-built copy below is the only one that
            # survives, regardless of how it got there.
            arcnames = {f"caio/{name}{ABI3_EXT_SUFFIX}" for name, _ in siblings}
            record_lines = [
                line for line in src.read(record_name).decode("utf-8").splitlines()
                if line.split(",", 1)[0] not in arcnames
            ]

            with zipfile.ZipFile(tmp_path, "w") as dst:
                for info in src.infolist():
                    if info.filename == record_name or info.filename in arcnames:
                        continue  # RECORD rewritten below, once, last;
                                  # stale sibling .so dropped (see above)
                    dst.writestr(info, src.read(info.filename))

                for name, so_path in siblings:
                    arcname = f"caio/{name}{ABI3_EXT_SUFFIX}"
                    zi = zipfile.ZipInfo.from_file(str(so_path), arcname=arcname)
                    zi.compress_type = so_compress_type
                    data = so_path.read_bytes()
                    dst.writestr(zi, data)
                    digest = base64.urlsafe_b64encode(
                        hashlib.sha256(data).digest()
                    ).rstrip(b"=").decode("ascii")
                    record_lines.append(f"{arcname},sha256={digest},{len(data)}")

                dst.writestr(record_info, "\n".join(record_lines) + "\n")
        tmp_path.replace(wheel_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    with tempfile.TemporaryDirectory(prefix="caio-primary-wheel-") as tmp:
        filename = maturin.build_wheel(tmp, config_settings, metadata_directory)
        wheel_path = Path(tmp) / filename
        siblings = build_siblings(Path(tmp))
        if siblings:
            inject_siblings_into_wheel(wheel_path, siblings)
        shutil.copy2(wheel_path, Path(wheel_directory) / filename)
    return filename


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    # maturin's own editable build drops thread_aio.abi3.so directly into
    # the real in-tree caio/ dir (python-source = "." means the source
    # tree itself is the install target) - the siblings get the same
    # treatment here, no zip involved at all for this path.
    filename = maturin.build_editable(wheel_directory, config_settings, metadata_directory)
    build_siblings(CAIO_DIR)
    return filename
