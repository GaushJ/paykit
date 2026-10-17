FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY paykit ./paykit
COPY migrations ./migrations
COPY alembic.ini ./

RUN pip install --no-cache-dir ".[service]"

EXPOSE 8000

CMD ["uvicorn", "paykit.service_app.main:app", "--host", "0.0.0.0", "--port", "8000"]
