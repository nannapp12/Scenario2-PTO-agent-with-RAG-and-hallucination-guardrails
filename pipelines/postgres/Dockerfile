# One image for both PostgreSQL pipelines (employee sync, handbook ingestion).
# Build from the repo root:  docker build -f pipelines/postgres/Dockerfile -t pto-pipelines .
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pipelines/postgres/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common /common
COPY pipelines/postgres/*.py ./
ENV PYTHONPATH=/
RUN useradd -m pipeline
USER pipeline
CMD ["python", "sync_employees.py"]
