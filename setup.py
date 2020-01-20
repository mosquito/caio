from setuptools import find_packages, Extension, setup


setup(
    name='linux_aio',
    version='0.0.1',
    packages=find_packages(exclude=['tests']),
    ext_modules=[
        Extension('linux_aio.eventfd', ['src/eventfd.c']),
        Extension('linux_aio.operation', ['src/operation.c']),
        Extension('linux_aio.context', ['src/context.c']),
    ]
)
