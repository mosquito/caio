import platform
from setuptools import Extension, setup


OS_NAME = platform.system().lower()
extensions = []


if "darwin" in OS_NAME or "linux" in OS_NAME:
    extensions.append(
        Extension(
            "caio.thread_aio",
            [
                "caio/thread_aio.c",
                "caio/src/lfqueue/lfq.c",
            ],
            extra_compile_args=["-g"],
            include_dirs=["caio/src/lfqueue"],
        ),
    )
if "linux" in OS_NAME:
    extensions.append(
        Extension(
            "caio.linux_aio", ["caio/linux_aio.c"], extra_compile_args=["-g"]
        ),
    )


setup(
    name="caio",
    version="0.0.4",
    packages=["caio"],
    package_data={"caio": [
        "caio/linux_aio.pyi",
        "caio/thread_aio.pyi"
    ]},
    long_description=open("README.rst").read(),
    ext_modules=extensions,
    include_package_data=True,
)
