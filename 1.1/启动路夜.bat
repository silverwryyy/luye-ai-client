@echo off
rem ============================================================
rem  Luye v1.0 launcher  --  ASCII only (cmd mangles UTF-8 bat)
rem
rem  Why the pre-flight checks below:
rem  the app runs under pythonw.exe, which has NO console window.
rem  Any failure while importing therefore exits silently and all
rem  the user sees is a blink. So the venv is tested here, in a
rem  visible console, where the error can actually be shown.
rem ============================================================
cd /d "%~dp0"
setlocal EnableExtensions
title Luye v1.0

rem ---- 1. is Luye installed on this PC? ----
if not exist ".venv\Scripts\python.exe" goto no_venv
if not exist "desktop_bug.py" goto no_main

rem ---- 2. do the packages import cleanly? ----
rem  the import list mirrors the top of desktop_bug.py.
rem  "||" catches ANY non-zero exit; the two errorlevel lines are
rem  belt-and-braces, because a crash code like 0xC0000135 comes
rem  back NEGATIVE and a plain "if errorlevel 1" would miss it.
".venv\Scripts\python.exe" -c "import requests, psutil, dotenv, openai, winotify, tkinter" 1>"launch_error.log" 2>&1 || goto bad_deps
if errorlevel 1 goto bad_deps
if not errorlevel 0 goto bad_deps
if exist "launch_error.log" del "launch_error.log" >nul 2>&1

rem ---- 3. is an API key filled in?  warning only, still launches ----
if not exist ".env" goto no_key
findstr /c:"sk-" ".env" >nul 2>&1
if errorlevel 1 goto no_key

:launch
start "" ".venv\Scripts\pythonw.exe" "desktop_bug.py"
exit /b 0

rem ============= failure / warning paths =============

:no_key
echo.
echo  [!] .env does not look like it holds a real API key yet.
echo      Open it with Notepad and paste the keys you applied for:
echo        DeepSeek :  https://platform.deepseek.com/
echo        Tavily   :  https://tavily.com/
echo      Luye will still open, but it cannot chat until a key is set.
echo.
pause
goto launch

:no_venv
echo.
echo  [X] .venv not found - Luye has not been installed on this PC.
echo.
echo      Run the INSTALLER first: double-click the other .bat file in
echo      this folder (the Chinese-named one, not this launcher).
echo      .bat files present here:
echo.
dir /b *.bat | findstr /v /i "_auto_python"
echo.
pause
exit /b 1

:bad_deps
echo.
echo  [X] Python could not be started, or the packages failed to import.
echo      Recorded in launch_error.log, last lines below:
echo.
type "launch_error.log"
echo.
echo      (If nothing was printed above, python.exe itself failed to
echo      start - the .venv is broken and has to be rebuilt.)
echo.
echo      Usual fix: run the INSTALLER again (the Chinese-named .bat in
echo      this folder), let it finish completely, then retry this file.
echo.
pause
exit /b 1

:no_main
echo.
echo  [X] desktop_bug.py is missing - this folder is incomplete.
echo      Copy the whole Luye folder again.
echo.
pause
exit /b 1
