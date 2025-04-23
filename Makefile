build: sdist mac_wheel linux_wheel

sdist:
	python3 setup.py sdist

mac_wheel:
	python3.9 -m build
	python3.10 -m build
	python3.11 -m build
	python3.12 -m build
	python3.13 -m build


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
