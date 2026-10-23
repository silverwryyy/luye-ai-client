@echo off
rem ============================================================
rem  Luye v1.0 python finder  --  ASCII only on purpose
rem  Contract with the installer:
rem    success -> %PY% defined, either  py -3  or  "C:\...\python.exe"
rem               (when a full path is used, the quotes are part of %PY%)
rem    failure -> errorlevel 1 and %PY% left undefined
rem  No setlocal in here: %PY% has to survive back into the caller.
rem ============================================================

set "PY="
set "MINCHK=import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)"

echo [1/5] Looking for the py launcher ...
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "%MINCHK%" >nul 2>nul
    if not errorlevel 1 (
        set "PY=py -3"
        goto :found
    )
)

echo [2/5] Looking for python on PATH ...
where python >nul 2>nul
if not errorlevel 1 (
    python -c "%MINCHK%" >nul 2>nul
    if not errorlevel 1 (
        set "PY=python"
        goto :found
    )
)

echo [3/5] Looking for an existing per-user install ...
call :scan_local
if defined PY goto :found

rem ---- 4) winget, then rescan ----
echo [4/5] Trying winget ...
where winget >nul 2>nul
if errorlevel 1 (
    echo       winget not available, skipping.
    goto :curl_fallback
)
winget install --id Python.Python.3.13 -e --source winget --scope user ^
    --accept-package-agreements --accept-source-agreements
call :scan_local
if defined PY goto :found

:curl_fallback
rem ---- 5) official silent installer ----
echo [5/5] Downloading the official Python 3.13 installer ...
set "EXE=%TEMP%\luye_python313.exe"
where curl >nul 2>nul
if not errorlevel 1 (
    curl -L -o "%EXE%" "https://www.python.org/ftp/python/3.13.0/python-3.13.0-amd64.exe" >nul 2>nul
)
if exist "%EXE%" (
    "%EXE%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0
    call :scan_local
    if defined PY goto :found
)

echo.
echo  [X] Could not find or install a Python 3.10+ runtime.
echo      Please install Python 3.13 from python.org and run this again.
echo.
exit /b 1

:found
echo.
echo  [OK] Using: %PY%
exit /b 0

rem ---- helper: probe %LOCALAPPDATA%\Programs\Python\Python31x ----
:scan_local
for %%D in (313 312 311 310) do (
    if not defined PY (
        if exist "%LOCALAPPDATA%\Programs\Python\Python%%D\python.exe" (
            "%LOCALAPPDATA%\Programs\Python\Python%%D\python.exe" -c "%MINCHK%" >nul 2>nul
            if not errorlevel 1 set PY="%LOCALAPPDATA%\Programs\Python\Python%%D\python.exe"
        )
    )
)
exit /b 0
