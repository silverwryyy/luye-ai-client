@echo off
rem ============================================================
rem  Luye v1.0 one-click installer  --  ASCII only on purpose
rem  Chinese text lives in install_setup.py (cmd mangles UTF-8 bat)
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
setlocal EnableExtensions
title Luye v1.0 Installer

rem ---- step 1: make sure this PC has a Python 3 ----
rem _auto_python.bat checks py/python, then winget, then the
rem official silent installer, and hands the answer back in %PY%
rem (either  py -3  or  "C:\...\python.exe"  - quotes included).
call "_auto_python.bat"
if errorlevel 1 exit /b 1
if not defined PY (
    echo.
    echo  [X] Python is still missing. Aborting.
    echo.
    pause
    exit /b 1
)

%PY% "install_setup.py"
set "RC=%ERRORLEVEL%"

echo.
pause
exit /b %RC%
