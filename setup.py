from setuptools import Extension, setup


setup(
    name="caio",
    version="0.0.3",
    packages=["caio"],
    package_data={"caio": ["caio/linux_aio.pyi"],},
    ext_modules=[
        Extension(
            "caio.linux_aio", ["caio/linux_aio.c"], extra_compile_args=["-g"]
        ),
    ],
)
