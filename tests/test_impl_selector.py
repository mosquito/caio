import os
import platform
import sys
from subprocess import check_output

import caio
import pytest


@pytest.fixture(params=caio.variants)
def implementation(request):
    if request.param is caio.linux_aio:
        return "linux"
    if request.param is caio.thread_aio:
        return "thread"
    if request.param is caio.python_aio:
        return "python"

    raise RuntimeError("Unknown variant %r" % (request.param,))


@pytest.mark.skipif(platform.system() == 'Windows', reason="Windows skip")
def test_env_selector(implementation):
    output = check_output(
        [
            sys.executable,
            "-c",
            "import caio, inspect; print(caio.Context.__doc__)"
        ],
        env={"CAIO_IMPL": implementation}
    ).decode()

    assert implementation in output, output


@pytest.fixture()
def implementation_file(implementation):
    path = os.path.dirname(caio.__file__)
    fname = os.path.join(path, "default_implementation")

    try:
        with open(fname, "w") as fp:
            fp.write("# NEWER COMMIT THIS FILE")
            fp.write("\nwrong string\n")
            fp.write(implementation)
            fp.write("\n\n")
        yield implementation
    finally:
        os.remove(fname)


def test_file_selector(implementation_file):
    output = check_output(
        [
            sys.executable,
            "-c",
            "import caio, inspect; print(caio.Context.__doc__)"
        ],
    ).decode()

    assert implementation_file in output, output
