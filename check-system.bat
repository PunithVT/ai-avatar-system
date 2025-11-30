@echo off
setlocal enabledelayedexpansion

echo ==========================================
echo   AI Avatar System - System Check
echo ==========================================
echo.

set PASSED=0
set FAILED=0

:: Test 1: Docker
echo [TEST 1/10] Checking Docker...
docker --version >nul 2>&1
if errorlevel 1 (
    echo [FAIL] Docker is not installed or not in PATH
    set /a FAILED+=1
) else (
    echo [PASS] Docker is installed
    set /a PASSED+=1
)

:: Test 2: Docker Compose
echo [TEST 2/10] Checking Docker Compose...
docker-compose --version >nul 2>&1
if errorlevel 1 (
    echo [FAIL] Docker Compose is not installed
    set /a FAILED+=1
) else (
    echo [PASS] Docker Compose is installed
    set /a PASSED+=1
)

:: Test 3: Docker Running
echo [TEST 3/10] Checking if Docker is running...
docker info >nul 2>&1
if errorlevel 1 (
    echo [FAIL] Docker is not running
    set /a FAILED+=1
) else (
    echo [PASS] Docker is running
    set /a PASSED+=1
)

:: Test 4: Environment File
echo [TEST 4/10] Checking .env file...
if not exist .env (
    echo [FAIL] .env file not found
    echo [INFO] Run: copy .env.example .env
    set /a FAILED+=1
) else (
    echo [PASS] .env file exists
    set /a PASSED+=1
)

:: Test 5: Docker Images
echo [TEST 5/10] Checking Docker images...
docker images | findstr avatar >nul 2>&1
if errorlevel 1 (
    echo [WARN] Avatar images not built yet
    echo [INFO] Run: docker-compose build
) else (
    echo [PASS] Docker images exist
    set /a PASSED+=1
)

:: Test 6: Services Running
echo [TEST 6/10] Checking if services are running...
docker-compose ps | findstr "Up" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Services are not running
    echo [INFO] Run: docker-compose up -d
) else (
    echo [PASS] Services are running
    set /a PASSED+=1
)

:: Test 7: Backend Health
echo [TEST 7/10] Checking backend health...
curl -s http://localhost:8000/health >nul 2>&1
if errorlevel 1 (
    echo [WARN] Backend not responding (may still be starting)
) else (
    echo [PASS] Backend is healthy
    set /a PASSED+=1
)

:: Test 8: Frontend Access
echo [TEST 8/10] Checking frontend...
curl -s http://localhost:3000 >nul 2>&1
if errorlevel 1 (
    echo [WARN] Frontend not responding (may still be starting)
) else (
    echo [PASS] Frontend is accessible
    set /a PASSED+=1
)

:: Test 9: Database
echo [TEST 9/10] Checking PostgreSQL...
docker-compose ps postgres | findstr "Up" >nul 2>&1
if errorlevel 1 (
    echo [WARN] PostgreSQL not running
) else (
    echo [PASS] PostgreSQL is running
    set /a PASSED+=1
)

:: Test 10: Redis
echo [TEST 10/10] Checking Redis...
docker-compose ps redis | findstr "Up" >nul 2>&1
if errorlevel 1 (
    echo [WARN] Redis not running
) else (
    echo [PASS] Redis is running
    set /a PASSED+=1
)

echo.
echo ==========================================
echo   Test Results
echo ==========================================
echo   PASSED: !PASSED!/10
echo   FAILED: !FAILED!/10
echo ==========================================
echo.

if !FAILED! GTR 0 (
    echo [ACTION REQUIRED] Please fix the failed tests above
    echo.
    echo Common solutions:
    echo   1. Install Docker Desktop from https://www.docker.com/products/docker-desktop
    echo   2. Start Docker Desktop
    echo   3. Copy .env.example to .env and configure
    echo   4. Run: docker-compose up -d
) else (
    echo [SUCCESS] All critical tests passed!
    echo.
    echo Your system is ready to use:
    echo   - Frontend: http://localhost:3000
    echo   - Backend:  http://localhost:8000
    echo   - API Docs: http://localhost:8000/docs
)

echo.
echo Detailed Service Status:
docker-compose ps

echo.
pause
