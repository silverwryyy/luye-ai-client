@echo off
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
title PPT Master Downloader
cd /d "%~dp0"

echo.
echo =============================================================
echo   Getting PPT Master component (~55MB)
echo =============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found on PATH.
  echo         Install Python 3.10+ from https://www.python.org/downloads/
  echo         Remember to tick "Add python.exe to PATH", then run this again.
  echo.
  pause
  exit /b 1
)

python "%~dp0fetch_ppt_master.py" %*
echo.
pause
