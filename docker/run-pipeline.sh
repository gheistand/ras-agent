#!/bin/bash
# Run a single watershed through the pipeline in Docker
# Usage: ./docker/run-pipeline.sh --lon -88.5 --lat 40.2 --output /app/output/test --mock
docker-compose run --rm api python3 pipeline/orchestrator.py "$@"
