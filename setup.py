from setuptools import Extension, setup


setup(
    name='linux_aio',
    version='0.0.1',
    packages=[''],
    package_data={
        '': ['linux_aio.pyi'],
    },
    ext_modules=[
        Extension('linux_aio', ['linux_aio.c']),
    ]
)
