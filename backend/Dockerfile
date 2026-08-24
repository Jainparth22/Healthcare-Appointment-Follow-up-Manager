# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first for better layer caching. psycopg[binary] bundles
# libpq, and bcrypt/cryptography ship manylinux wheels, so no apt build deps.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Application code + migrations.
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

EXPOSE 8000

# Lightweight liveness probe (no curl needed on the slim image).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"

# Default command runs the web server; compose overrides it for worker/beat.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
