@echo off
cd /d "%~dp0\.."
title Bilibili Banner v11.0

python --version >nul 2>&1
if not errorlevel 1 (
  set "PY=python"
  goto havepython
)

py -3 --version >nul 2>&1
if not errorlevel 1 (
  set "PY=py -3"
  goto havepython
)

echo ERROR: Python 3 was not found.
pause
exit /b 1

:havepython
%PY% -c "import playwright" >nul 2>&1
if errorlevel 1 (
  echo Installing Python dependencies...
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 goto fail
)

echo [1/3] Fetching current banner from Header API...
%PY% backend\capture.py
if errorlevel 1 goto fail

echo [2/3] Building local static site...
%PY% scripts\build_site.py
if errorlevel 1 goto fail

echo [3/3] Starting preview server...
start "Bilibili Banner Preview" /min cmd /c "%PY% scripts\serve.py"
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:8765"
exit /b 0

:fail
echo.
echo FAILED. See data\diagnostic.json if present.
pause
exit /b 1
