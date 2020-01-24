from setuptools import Extension, setup


setup(
    name='linux_aio',
    version='0.2.0',
    packages=['linux_aio'],
    package_data={
        'linux_aio': ['linux_aio/_aio.pyi'],
    },
    ext_modules=[
        Extension('linux_aio._aio', ['linux_aio/_aio.c'],
                  extra_compile_args=["-g"]),
    ]
)
