#!/bin/bash
# Development reload script for the rebalancer service
# Usage: ./tools/reload.sh

echo "Reloading rebalancer..."
docker compose stop rebalancer && docker compose up -d --build rebalancer
