"""CLI: serve the API. (`commercial-credit-agent serve`)"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "serve":
        import uvicorn

        uvicorn.run("commercial_credit.api:app", host="0.0.0.0", port=8000)
        return 0
    print("usage: commercial-credit-agent serve")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
