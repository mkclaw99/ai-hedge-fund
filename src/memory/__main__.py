"""CLI for the research wiki: init, query, lint, log.

    poetry run python -m src.memory init
    poetry run python -m src.memory query AAPL
    poetry run python -m src.memory lint
    poetry run python -m src.memory log
"""

from __future__ import annotations

import sys

from src.memory.store import WikiMemory


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "help"
    wiki = WikiMemory()

    if cmd == "init":
        print(f"Wiki initialized at {wiki.root}")
        return 0

    if cmd == "query":
        if len(argv) < 2:
            print("usage: python -m src.memory query <TICKER>")
            return 2
        digest = wiki.render_context_for_prompt([argv[1].upper()])
        print(digest or f"No prior research on {argv[1].upper()}.")
        return 0

    if cmd == "lint":
        findings = wiki.lint()
        if not findings:
            print("No problems detected.")
            return 0
        print(f"{len(findings)} finding(s):")
        for f in findings:
            print(f"  - {f}")
        return 0

    if cmd == "log":
        log = wiki.root / "log.md"
        print(log.read_text(encoding="utf-8") if log.exists() else "(no log)")
        return 0

    print(__doc__)
    return 0 if cmd == "help" else 2


if __name__ == "__main__":
    raise SystemExit(main())
