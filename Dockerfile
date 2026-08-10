# Multi-arch (amd64 + arm64) — the production target is a Hetzner CAX11 ARM box.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin cpapi

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=cpapi:cpapi alembic.ini ./
COPY --chown=cpapi:cpapi migrations ./migrations

USER cpapi
EXPOSE 8000

# /healthz is process + DB only (design critique #14), so it is a safe container probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "crypto_processing_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
