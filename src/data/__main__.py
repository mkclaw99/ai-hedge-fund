"""CLI for the persistent Financial Datasets cache.

    poetry run python -m src.data stats
    poetry run python -m src.data clear [data_type]
"""

from __future__ import annotations

import sys

from src.data.persistent_cache import SQLiteCache


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "stats"
    cache = SQLiteCache()

    if cmd == "stats":
        stats = cache.stats()
        if not stats or stats.get("total", 0) == 0:
            print(f"Cache empty ({cache.path}).")
            return 0
        print(f"Cache at {cache.path}:")
        for dt, n in sorted(stats.items()):
            if dt != "total":
                print(f"  {dt:<20} {n}")
        print(f"  {'total':<20} {stats.get('total', 0)}")
        return 0

    if cmd == "clear":
        data_type = argv[1] if len(argv) > 1 else None
        n = cache.clear(data_type)
        print(f"Cleared {n} entr{'y' if n == 1 else 'ies'}"
              + (f" for {data_type}" if data_type else "") + ".")
        return 0

    print(__doc__)
    return 0 if cmd == "help" else 2


if __name__ == "__main__":
    raise SystemExit(main())
