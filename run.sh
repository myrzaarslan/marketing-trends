#!/usr/bin/env bash
# Build (if needed) and launch Marketing Trends locally, then open it.
# macOS / Linux. Requires Docker Desktop running.
set -euo pipefail
cd "$(dirname "$0")"

if ! docker info >/dev/null 2>&1; then
  echo "Docker isn't running. Please start Docker Desktop and try again."
  exit 1
fi

echo "Starting Marketing Trends…"
echo "(First run builds the image and downloads ~2GB — this can take several minutes.)"
docker compose up --build -d

URL="http://localhost:8001"
printf "Waiting for the app to be ready"
for _ in $(seq 1 90); do
  if curl -fsS "$URL/health" >/dev/null 2>&1; then echo " — ready!"; break; fi
  printf "."
  sleep 2
done

echo "Marketing Trends is running at: $URL"
if command -v open >/dev/null 2>&1; then open "$URL"
elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
fi
echo "To stop it later, run:  docker compose down"
