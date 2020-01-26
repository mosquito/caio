from setuptools import Extension, setup


setup(
    name='linux_aio',
    version='0.2.0',
    packages=['linux_aio'],
    package_data={
        'linux_aio': ['linux_aio/aio.pyi'],
    },
    ext_modules=[
        Extension('linux_aio.aio', ['linux_aio/aio.c'],
                  extra_compile_args=["-g"]),
    ]
)
