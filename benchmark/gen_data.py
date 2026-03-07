import hashlib
import os
import pathlib
from multiprocessing.pool import ThreadPool

from rich.progress import Progress

POOL = ThreadPool(32)

DATA_DIR = pathlib.Path(os.environ.get("CAIO_BENCH_DATA", "data"))


def gen_data(file_id):
    seed = os.urandom(64)
    hasher = hashlib.sha512()

    with open(DATA_DIR / f"{file_id}.bin", "wb+") as fp:
        for _ in range(100000):
            hasher.update(seed)
            seed = hasher.digest()
            fp.write(seed)


def main():
    files = 128
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with Progress() as progress:
        task = progress.add_task("Generating...", total=files)
        for _ in POOL.imap_unordered(gen_data, range(files)):
            progress.advance(task)


if __name__ == "__main__":
    main()
