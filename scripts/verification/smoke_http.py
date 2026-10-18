"""Read-only staging smoke test, with no case mutations or authentication tokens."""

import argparse
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def check(url: str) -> None:
    try:
        with urlopen(url, timeout=10) as response:
            if not 200 <= response.status < 400:
                raise RuntimeError(f"Unhealthy HTTP status {response.status} for {url}")
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"Smoke check failed for {url}: {exc}") from exc
    print(f"PASS: {url}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--web", default="http://127.0.0.1:3000")
    args = parser.parse_args()
    for path in ("/health", "/health/db"):
        check(args.api.rstrip("/") + path)
    check(args.web.rstrip("/") + "/")


if __name__ == "__main__":
    main()
