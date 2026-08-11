# Built from the repository root, because the image needs the SDK from the
# checkout next door rather than from PyPI:
#
#     docker build -f examples/platform-demo/Dockerfile -t platform-demo .
#
# The regtest compose file does this for you under the `example` profile.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN useradd --create-home --uid 10002 --shell /usr/sbin/nologin demo

# The two directories are laid out exactly as they are in the repository, so
# the relative `-e ../../sdks/python` in requirements.txt resolves here the same
# way it does on a laptop.
WORKDIR /src/examples/platform-demo
COPY sdks/python /src/sdks/python
COPY examples/platform-demo/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY examples/platform-demo/app.py ./
COPY examples/platform-demo/templates ./templates

USER demo
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
