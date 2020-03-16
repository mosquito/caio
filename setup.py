from setuptools import Extension, setup


setup(
    name="caio",
    version="0.0.4",
    packages=["caio"],
    package_data={"caio": ["caio/linux_aio.pyi"],},
    long_description=open("README.rst").read(),
    ext_modules=[
        Extension(
            "caio.linux_aio", ["caio/linux_aio.c"], extra_compile_args=["-g"]
        ),
    ],
)
