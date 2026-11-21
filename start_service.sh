#!/bin/bash
# Start the SetuHaul live agent control room service
set -e

cd "$(dirname "$0")"

# Activate venv
if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
fi

# Kill any existing service on port 8080
pkill -f "service.py" 2>/dev/null || true
sleep 1

# Start the service
echo "=== Starting SetuHaul Live Agent Control Room ==="
echo "Dashboard: http://127.0.0.1:8080/dashboard"
echo "SSE Stream: http://127.0.0.1:8080/stream"
echo "Health: http://127.0.0.1:8080/health"
echo ""
python3 service.py --port 8080 --db setuhaul_freight_operations.db
