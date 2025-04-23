import platform

from setuptools import Extension, setup


module_name = "caio"


OS_NAME = platform.system().lower()
extensions = []


if "linux" in OS_NAME:
    extensions.append(
        Extension(
            "{}.thread_aio".format(module_name),
            [
                "{}/thread_aio.c".format(module_name),
                "{}/src/threadpool/threadpool.c".format(module_name),
            ],
            extra_compile_args=["-g", "-DHAVE_FDATASYNC"],
        ),
    )
elif "darwin" in OS_NAME:
    extensions.append(
        Extension(
            "{}.thread_aio".format(module_name),
            [
                "{}/thread_aio.c".format(module_name),
                "{}/src/threadpool/threadpool.c".format(module_name),
            ],
            extra_compile_args=["-g"],
        ),
    )
if "linux" in OS_NAME:
    extensions.append(
        Extension(
            "{}.linux_aio".format(module_name),
            ["{}/linux_aio.c".format(module_name)],
            extra_compile_args=["-g"],
        ),
    )


setup(
    ext_modules=extensions,
)
