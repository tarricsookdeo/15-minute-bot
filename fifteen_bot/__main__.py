"""Start the server: `uv run python -m fifteen_bot` (opens the dashboard locally).

Hosted, it's started as `python -m fifteen_bot --host 0.0.0.0 --no-browser`, with
PORT and FORWARDED_ALLOW_IPS set by the environment (see the README, "Configuration").
"""

import argparse
import logging
import os
import threading
import webbrowser

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="15-minute market trading bot for Public.com")
    parser.add_argument("--host", default="127.0.0.1", help="interface to listen on (0.0.0.0 when hosted)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    parser.add_argument("--no-browser", action="store_true", help="don't open the dashboard automatically")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # The SDK's HTTP client logs every request URL at INFO; keep the logs to what matters.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    print(f"15-Minute Bot listening on {args.host}:{args.port}  (Ctrl+C to stop)")
    uvicorn.run(
        "fifteen_bot.app:app",
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,  # request logs add nothing here and could grow with many users
        # Behind a hosting platform's load balancer, trust its X-Forwarded-* headers so the
        # app sees the real client IP (for connection limits) and that the page is HTTPS.
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        # Open dashboards never close on their own; stop waiting for them after a few seconds,
        # then the app ends every session, cancelling unfilled orders, before exiting.
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
