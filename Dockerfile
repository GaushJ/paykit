# Build a wheel in one stage, install it into a slim runtime as a non-root user.
FROM python:3.12-slim AS build

WORKDIR /src
RUN pip install --no-cache-dir build
COPY pyproject.toml README.md LICENSE ./
COPY paykit ./paykit
RUN python -m build --wheel --outdir /dist


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# asyncpg, pydantic-core etc. ship manylinux wheels: no compiler needed.
COPY --from=build /dist/*.whl /tmp/
RUN WHEEL=$(ls /tmp/*.whl) && pip install "${WHEEL}[service]" && rm -f /tmp/*.whl \
    && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin paykit

USER paykit
WORKDIR /home/paykit
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)" || exit 1

# --proxy-headers: trust X-Forwarded-* from the load balancer in front of it.
CMD ["uvicorn", "paykit.service_app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
