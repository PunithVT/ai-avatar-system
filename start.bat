@echo off
echo ==========================================
echo   AI Avatar System - Quick Start
echo ==========================================
echo.

:: Check if Docker is running
docker info >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker is not running!
    echo Please start Docker Desktop and try again.
    pause
    exit /b 1
)

echo [1/5] Checking environment configuration...
if not exist .env (
    echo [INFO] Creating .env file from template...
    copy .env.example .env
    echo.
    echo [ACTION REQUIRED] Please edit .env file with your credentials:
    echo   - AWS_ACCESS_KEY_ID
    echo   - AWS_SECRET_ACCESS_KEY
    echo   - S3_BUCKET_NAME
    echo   - ANTHROPIC_API_KEY or OPENAI_API_KEY
    echo.
    notepad .env
    echo.
    echo Press any key once you've configured .env file...
    pause >nul
)

echo [2/5] Stopping any existing containers...
docker-compose down

echo [3/5] Building Docker images...
docker-compose build

echo [4/5] Starting services...
docker-compose up -d

echo [5/5] Waiting for services to be ready...
timeout /t 10 /nobreak >nul

echo.
echo ==========================================
echo   Services Started Successfully!
echo ==========================================
echo.
echo   Frontend:     http://localhost:3000
echo   Backend API:  http://localhost:8000
echo   API Docs:     http://localhost:8000/docs
echo   Flower:       http://localhost:5555
echo.
echo [INFO] Checking service status...
docker-compose ps

echo.
echo [INFO] To view logs, run:
echo   docker-compose logs -f
echo.
echo [INFO] To stop services, run:
echo   docker-compose down
echo.

:: Try to open the application in browser
echo Opening application in browser...
start http://localhost:3000

echo.
echo Press any key to exit...
pause >nul
