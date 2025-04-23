set -ex

mkdir -p dist

MACHINE=$(/opt/python/cp311-cp311/bin/python3 -c 'import platform; print(platform.machine())')

function build_wheel() {
  /opt/python/$1/bin/pip install build
	/opt/python/$1/bin/python -m build --wheel
}

build_wheel cp39-cp39
build_wheel cp310-cp310
build_wheel cp311-cp311
build_wheel cp312-cp312
build_wheel cp313-cp313

cd dist

for f in ./*-linux*_${MACHINE}*;
do if [ -f $f ]; then auditwheel repair $f -w . ; rm $f; fi;
done
