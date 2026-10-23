@echo off
rem ============================================================
rem  Luye v1.1 optional-components installer  --  ASCII only on purpose
rem  Chinese text lives in install_optional.py (cmd mangles UTF-8 bat)
rem
rem  Optional means optional: skipping this file costs you nothing,
rem  Luye still chats fine without OfficeCLI or ppt-master.
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
setlocal EnableExtensions
title Luye v1.1 - Optional Components

rem ---- Python is needed even for the optional bits ----
rem _auto_python.bat checks py/python, then winget, then the official
rem silent installer, and hands the answer back in %PY%.
call "_auto_python.bat"
if errorlevel 1 exit /b 1
if not defined PY (
    echo.
    echo  [X] Python is still missing, so nothing can be downloaded yet.
    echo      Run the MAIN installer first - it sets Python up for you.
    echo.
    pause
    exit /b 1
)

%PY% "install_optional.py" %*
set "RC=%ERRORLEVEL%"

echo.
pause
exit /b %RC%
