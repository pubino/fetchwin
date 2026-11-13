# syntax=docker/dockerfile:1
# Pinned toolchain: Debian 13 (trixie) + wimtools + genisoimage + bsdtar.
# Bump digests/versions deliberately; CI will tell you if a pin has vanished upstream.

FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS base
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      wimtools=1.14.4-1.1+b3 \
      genisoimage=9:1.1.11-4 \
      libarchive-tools=3.7.4-4+deb13u1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/venv/bin:$PATH
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# Lint, type-check and the full test suite (including real-tool integration tests).
FROM base AS test
RUN uv sync --frozen
COPY tests ./tests
COPY prototypes ./prototypes
CMD ["sh", "-c", "ruff check . && ruff format --check . && mypy && pytest -v"]

# The CLI itself. Mount an output directory at /out.
FROM base AS runtime
WORKDIR /out
ENTRYPOINT ["fetchwin"]
CMD ["--help"]
