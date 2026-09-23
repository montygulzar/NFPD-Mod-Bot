"""Docker HEALTHCHECK probe.

Run as `python healthcheck.py`. Exits 0 when the bot reports ready, 1 otherwise.

Uses only the standard library so it works in the slim image without adding curl
or wget, and reads HEALTH_PORT from the environment so it follows the app's config
rather than duplicating a port number.
"""
import os
import sys
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 4.0


def main() -> int:
    if os.environ.get("HEALTH_SERVER_ENABLED", "true").strip().lower() in {"0", "false", "no", "off"}:
        # Nothing to probe; treat the container as healthy rather than flapping.
        return 0

    port = os.environ.get("HEALTH_PORT", "8080").strip() or "8080"
    url = f"http://127.0.0.1:{port}/ready"

    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            if response.status == 200:
                return 0
            print(f"not ready: HTTP {response.status}", file=sys.stderr)
            return 1
    except urllib.error.HTTPError as error:
        # 503 is the readiness endpoint deliberately reporting a degraded state.
        body = error.read(500).decode("utf-8", "replace")
        print(f"not ready: HTTP {error.code} {body}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"health probe failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
