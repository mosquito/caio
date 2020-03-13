from setuptools import Extension, setup


setup(
    name='caio',
    version='0.3.0',
    packages=['caio'],
    package_data={
        'caio': ['caio/aio.pyi'],
    },
    ext_modules=[
        Extension('caio.aio', ['caio/aio.c'], extra_compile_args=["-g"]),
    ]
)
