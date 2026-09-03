# syntax=docker/dockerfile:1.7
# ---- build: resolve and install dependencies with uv -------------------------------------
FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first so they cache independently of source changes: this layer only re-runs when
# pyproject.toml or uv.lock change, which is the caching that matters. No `--mount=type=cache`
# here — Railway's builder demands its own cacheKey prefix in the mount id, and the layer cache
# already covers the expensive sync.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini README.md ./
RUN uv sync --locked --no-dev

# ---- runtime: slim image, non-root, no uv ----------------------------------------------
FROM python:3.14-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH" \
    LOG_FORMAT=json TZ=UTC
RUN apt-get update \
    && apt-get install -y --no-install-recommends libheif1 tini ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system strikt && useradd --system --gid strikt --home /app --shell /usr/sbin/nologin strikt
WORKDIR /app
COPY --from=build --chown=strikt:strikt /app/.venv /app/.venv
COPY --from=build --chown=strikt:strikt /app/src /app/src
COPY --from=build --chown=strikt:strikt /app/migrations /app/migrations
COPY --from=build --chown=strikt:strikt /app/alembic.ini /app/alembic.ini
USER strikt
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status == 200 else 1)"
ENTRYPOINT ["tini", "--"]
CMD ["sh", "-c", "alembic upgrade head && exec python -m strikt"]
