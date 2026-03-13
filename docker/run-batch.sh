#!/bin/bash
# Run a batch job in Docker
# Usage: ./docker/run-batch.sh watersheds.csv /app/output --mock
docker-compose run --rm api python3 pipeline/batch.py "$@"
