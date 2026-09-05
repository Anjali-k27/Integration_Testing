#!/usr/bin/env bash
set -euo pipefail

echo "Checking fastapi gateway..."
curl -sf http://localhost:8080/health > /dev/null && echo "  fastapi: healthy"

echo "Cluster is up."
