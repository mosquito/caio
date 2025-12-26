build: sdist mac_wheel linux_wheel

.PHONY: sdist mac_wheel linux_wheel

sdist:
	python3 setup.py sdist

.venvs:
	mkdir -p $@

.venvs/3.10: .venvs
	python3.10 -m venv $@
	$@/bin/python -m pip install -U pip setuptools build wheel

.venvs/3.11: .venvs
	python3.11 -m venv $@
	$@/bin/python -m pip install -U pip setuptools build wheel

.venvs/3.12: .venvs
	python3.12 -m venv $@
	$@/bin/python -m pip install -U pip setuptools build wheel

.venvs/3.13: .venvs
	python3.13 -m venv $@
	$@/bin/python -m pip install -U pip setuptools build wheel

.venvs/3.14: .venvs
	python3.14 -m venv $@
	$@/bin/python -m pip install -U pip setuptools build wheel

mac_wheel: .venvs/3.10 .venvs/3.11 .venvs/3.12 .venvs/3.13 .venvs/3.14
	.venvs/3.10/bin/python -m build
	.venvs/3.11/bin/python -m build
	.venvs/3.12/bin/python -m build
	.venvs/3.13/bin/python -m build
	.venvs/3.14/bin/python -m build

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
