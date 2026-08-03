set -ex

mkdir -p dist

MACHINE=$(/opt/python/cp310-cp310/bin/python3 -c 'import platform; print(platform.machine())')

# manylinux containers don't ship Rust; install it once for the whole build.
if ! command -v cargo >/dev/null 2>&1; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
fi
source "$HOME/.cargo/env"

# musllinux's *-unknown-linux-musl Rust targets default to crt-static (fully
# static linking), which can't produce a cdylib - disable it. manylinux
# (glibc) doesn't need this.
if ldd --version 2>&1 | grep -qi musl; then
  export RUSTFLAGS="-C target-feature=-crt-static"
fi

# All three backends build against PyO3's abi3-py310 (stable ABI, floor
# 3.10), so a single build covers every supported CPython version - no
# per-version loop needed. scripts/build_backend.py (the PEP 517 backend
# - see pyproject.toml) builds thread_aio via maturin, then additionally
# builds the linux_aio/linux_uring sibling crates itself via plain `cargo
# build` and merges their .abi3.so into the resulting wheel.
/opt/python/cp310-cp310/bin/pip install -U build
/opt/python/cp310-cp310/bin/python -m build --wheel

cd dist

for f in ./*-linux*_${MACHINE}*;
do if [ -f $f ]; then auditwheel repair $f -w . ; rm $f; fi;
done
