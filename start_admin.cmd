@echo off
setlocal
cd /d "%~dp0"
title Bilibili Banner Local Admin

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
  echo ERROR: Project dependencies are missing.
  echo Run: %PY% -m pip install -r requirements.txt
  pause
  exit /b 1
)

%PY% scripts\admin_server.py %*
if errorlevel 1 goto fail
exit /b 0

:fail
echo.
echo The local Banner manager could not start. Check the error above.
pause
exit /b 1
