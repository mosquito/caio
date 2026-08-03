build: sdist mac_wheel linux_wheel

.PHONY: sdist mac_wheel linux_wheel clean

# Builds via scripts/build_backend.py (the PEP 517 backend, see
# pyproject.toml) - a thin wrapper around maturin, which packages the Rust
# sources plus the pure-Python tree; requires the `build` package (pip
# install build) and a Rust toolchain (cargo) on PATH.
sdist:
	python3 -m build --sdist

.venvs:
	mkdir -p $@

.venvs/3.10: .venvs
	python3.10 -m venv $@
	$@/bin/python -m pip install -U pip setuptools build wheel

# All three backends build against PyO3's abi3-py310 (stable ABI, floor
# 3.10), so one build covers every supported CPython version (3.10-3.14) -
# no per-version venv/build loop needed. Requires a Rust toolchain (cargo)
# on PATH - `python -m build`'s isolated builder installs maturin itself
# (scripts/build_backend.py's own [build-system] requires), but not Rust.
mac_wheel: .venvs/3.10
	.venvs/3.10/bin/python -m build

linux_wheel:
	docker run -it --rm \
		-v `pwd`:/mnt \
		--entrypoint /bin/bash \
		--workdir /mnt \
		--platform linux/amd64 \
		quay.io/pypa/manylinux_2_34_x86_64 \
		scripts/make-wheels.sh

	docker run -it --rm \
		-v `pwd`:/mnt \
		--entrypoint /bin/bash \
		--platform linux/arm64 \
		--workdir /mnt \
		quay.io/pypa/manylinux_2_34_aarch64 \
		scripts/make-wheels.sh

	docker run -it --rm \
		-v `pwd`:/mnt \
		--entrypoint /bin/bash \
		--workdir /mnt \
		--platform linux/amd64 \
		quay.io/pypa/musllinux_1_2_x86_64 \
		scripts/make-wheels.sh

	docker run -it --rm \
		-v `pwd`:/mnt \
		--entrypoint /bin/bash \
		--platform linux/arm64 \
		--workdir /mnt \
		quay.io/pypa/musllinux_1_2_aarch64 \
		scripts/make-wheels.sh

# Removes every build artifact this Makefile/build_backend.py can produce,
# including native extensions - not just `cargo clean`'s own `target/`,
# which leaves the compiled `caio/*.abi3.so` files (and
# scripts/build_backend.py's own `target/siblings` sibling-crate output)
# sitting around. Run this before a release build: a stale or
# wrong-architecture `.so` left in `caio/` (e.g. from switching
# platforms/architectures between builds) gets silently bundled into the
# next wheel via maturin's own `python-source = "."` handling otherwise.
clean:
	rm -rf .venvs build dist target caio.egg-info
	rm -f caio/*.abi3.so
