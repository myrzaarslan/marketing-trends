@echo off
REM Build (if needed) and launch Marketing Trends locally, then open it.
REM Windows. Requires Docker Desktop running.
cd /d "%~dp0"

docker info >nul 2>&1
if errorlevel 1 (
  echo Docker isn't running. Please start Docker Desktop and try again.
  pause
  exit /b 1
)

echo Starting Marketing Trends...
echo (First run builds the image and downloads ~2GB - this can take several minutes.)
docker compose up --build -d
if errorlevel 1 (
  echo Failed to start. See the output above.
  pause
  exit /b 1
)

echo Waiting for the app to be ready...
timeout /t 25 /nobreak >nul

echo Marketing Trends is running at: http://localhost:8001
start "" http://localhost:8001
echo To stop it later, run:  docker compose down
pause
