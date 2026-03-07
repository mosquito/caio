Python wrapper for AIO
======================

Python bindings for async file I/O on Linux and a pure-Python/thread fallback
for other platforms.

Three backends are available:

| Backend         | Kernel | Notes                                                      |
|-----------------|--------|------------------------------------------------------------|
| `linux_uring`   | ≥ 5.6  | io_uring - shared ring buffers, zero-syscall completions   |
| `linux_aio`     | ≥ 4.18 | kernel AIO (`io_submit` / `io_getevents`), `O_DIRECT` only |
| `thread_aio`    | any    | pthreads pool, portable                                    |
| `python_aio`    | any    | pure Python, no C extension required                       |

Example
-------

```python
import asyncio
from caio import AsyncioContext

loop = asyncio.get_event_loop()

async def main():
    # max_requests=128 by default
    ctx = AsyncioContext(max_requests=128)

    with open("test.file", "wb+") as fp:
        fd = fp.fileno()

        # Execute one write operation
        await ctx.write(b"Hello world", fd, offset=0)

        # Execute one read operation
        print(await ctx.read(32, fd, offset=0))

        # Execute one fdsync operation
        await ctx.fdsync(fd)

        op1 = ctx.write(b"Hello from ", fd, offset=0)
        op2 = ctx.write(b"async world", fd, offset=11)

        await asyncio.gather(op1, op2)

        print(await ctx.read(32, fd, offset=0))
        # Hello from async world


loop.run_until_complete(main())
```

Selecting a backend
-------------------

`from caio import AsyncioContext` picks the best available backend automatically
(`linux_uring` → `linux_aio` → `thread_aio` → `python_aio`).

To force a specific backend use the `CAIO_IMPL` environment variable:

```bash
CAIO_IMPL=uring   python my_app.py   # linux_uring
CAIO_IMPL=linux   python my_app.py   # linux_aio
CAIO_IMPL=thread  python my_app.py   # thread_aio
CAIO_IMPL=python  python my_app.py   # python_aio
```

Or import a backend directly:

```python
# io_uring (Linux ≥ 5.6)
from caio.linux_uring_asyncio import AsyncioContext

# kernel AIO (Linux ≥ 4.18)
from caio.linux_aio_asyncio import AsyncioContext

# thread pool
from caio.thread_aio_asyncio import AsyncioContext
```

A `default_implementation` file placed next to `caio/__init__.py` (useful for
distro package maintainers) may contain one of `uring`, `linux`, `thread`, or
`python` on its first non-comment line.

Troubleshooting
---------------

### io_uring blocked by seccomp

Containers (Docker, Podman, Kubernetes) often block io_uring via a seccomp
filter. The import will raise `ImportError` with a diagnostic message if
`io_uring_setup(2)` returns `ENOSYS`.

Fix:

```bash
# Docker / Podman
docker run --security-opt seccomp=unconfined ...

# Kubernetes
securityContext:
  seccompProfile:
    type: Unconfined
```

### linux_aio compatibility

`linux_aio` requires kernel ≥ 4.18 and a compatible filesystem.  If it does
not work in your environment you can fall back to `thread` or `python`:

```bash
CAIO_IMPL=thread python my_app.py
```
