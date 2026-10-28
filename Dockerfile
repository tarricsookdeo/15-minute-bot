# syntax=docker/dockerfile:1
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app
# Dependencies first, exactly as locked, so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY fifteen_bot ./fifteen_bot

# Run without root privileges; the app never writes to disk.
RUN useradd --system --uid 10001 --no-create-home bot
USER bot

# The hosting platform terminates HTTPS and forwards requests; trust its X-Forwarded-* headers.
# Only safe because the container is reachable solely through that load balancer.
ENV PORT=8080 \
    FORWARDED_ALLOW_IPS="*"
EXPOSE 8080

CMD ["/app/.venv/bin/python", "-m", "fifteen_bot", "--host", "0.0.0.0", "--no-browser"]
