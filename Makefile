build: sdist mac_wheel linux_wheel

sdist:
	python3 setup.py sdist

mac_wheel:
	python3.5 setup.py bdist_wheel
	python3.6 setup.py bdist_wheel
	python3.7 setup.py bdist_wheel
	python3.8 setup.py bdist_wheel

