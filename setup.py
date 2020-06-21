import os
import platform
from importlib.machinery import SourceFileLoader

from setuptools import Extension, setup


module_name = "caio"
module = SourceFileLoader(
    "version", os.path.join(module_name, "version.py"),
).load_module()


OS_NAME = platform.system().lower()
extensions = []


if "darwin" in OS_NAME or "linux" in OS_NAME:
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
    name=module_name,
    version=module.__version__,
    ext_modules=extensions,
    include_package_data=True,
    description=module.package_info,
    long_description=open("README.rst").read(),
    license=module.package_license,
    author=module.__author__,
    author_email=module.team_email,
    package_data={
        module_name: [
            "{}/linux_aio.pyi".format(module_name),
            "{}/thread_aio.pyi".format(module_name),
            "py.typed",
        ],
    },
    project_urls={
        "Documentation": "https://github.com/mosquito/caio/",
        "Source": "https://github.com/mosquito/caio",
    },
    packages=[module_name],
    classifiers=[
        "License :: OSI Approved :: Apache Software License",
        "Topic :: Software Development",
        "Topic :: Software Development :: Libraries",
        "Intended Audience :: Developers",
        "Natural Language :: English",
        "Operating System :: MacOS",
        "Operating System :: POSIX",
        "Operating System :: Microsoft",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.5",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: Implementation :: CPython",
    ],
    python_requires=">=3.5.*, <4",
    extras_require={
        "develop": [
            "aiomisc",
            "pytest",
            "pytest-cov",
        ],
    },
)
