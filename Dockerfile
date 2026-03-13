FROM python:3.11-slim

# Install system geospatial deps (same as CI)
RUN apt-get update && apt-get install -y \
    libgdal-dev gdal-bin libgeos-dev libproj-dev \
    dos2unix git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY pipeline/requirements.txt requirements.txt
RUN pip install --upgrade pip && \
    pip install gdal==$(gdal-config --version) && \
    pip install -r requirements.txt

# Copy pipeline source
COPY pipeline/ pipeline/
COPY docs/ docs/

# Data directory for SQLite job queue
RUN mkdir -p data/logs

ENV JOBS_DB_PATH=/app/data/jobs.db
ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["python3", "pipeline/api.py"]
