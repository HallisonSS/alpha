FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY agent/requirements.txt ./requirements.txt

RUN pip install --no-cache-dir -r requirements.txt

COPY agent/app ./app
COPY agent/worker ./worker
COPY db ./db

RUN useradd --system --uid 10001 --create-home agent \
    && chown -R agent:agent /app
    && mkdir -p /kiln/workspaces \
    && chown -R 10001:10001 /app /kiln

USER agent

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
