import os

import tqdm
import hashlib
from multiprocessing.pool import ThreadPool


POOL = ThreadPool(32)


def gen_data(file_id):
    seed = os.urandom(64)
    hasher = hashlib.sha512()

    with open(f"data/{file_id}.bin", "wb+") as fp:
        for _ in range(100000):
            hasher.update(seed)
            seed = hasher.digest()
            fp.write(seed)


def main():
    files = 128

    iterator = tqdm.tqdm(
        POOL.imap_unordered(gen_data, range(files)), total=files
    )

    for _ in iterator:
        pass


if __name__ == "__main__":
    main()
