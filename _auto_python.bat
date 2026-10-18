@echo off
rem ============================================================
rem  Luye v1.0  --  make sure this PC has a usable Python 3
rem
rem  Called from "zhuang-wo.bat" (the installer) with CALL.
rem  NO setlocal in here on purpose: the answer is handed back
rem  to the caller in %PY%, which is either
rem        py -3
rem  or    "C:\...\Python312\python.exe"   (quotes included!)
rem  so the caller can always just write   %PY% "script.py"
rem
rem  ASCII only on purpose: cmd mangles UTF-8 .bat files.
rem  All Chinese messages come back as soon as Python exists.
rem
rem  Levels: 1) py -3     2) python      3) winget
rem          4) python.org silent installer      5) give up
rem ============================================================

rem ---------- level 1 + 2 : is it already here? ----------
set "PY="
py -3 -V >nul 2>nul && set "PY=py -3"
if not defined PY (
    python -V >nul 2>nul && set "PY=python"
)
if defined PY goto :done

echo.
echo  ------------------------------------------------------------------
echo   Python is not on this PC yet.
echo   No worries - I will set it up for you, about 1-3 minutes.
echo   (Chinese messages come back right after this step.)
echo  ------------------------------------------------------------------

rem ---------- level 3 : winget (no admin rights needed) ----------
set "HAVE_WINGET="
where winget >nul 2>nul && set "HAVE_WINGET=1"
if not defined HAVE_WINGET echo   [2/3] winget not available on this PC, skip.
if defined HAVE_WINGET (
    echo   [2/3] trying winget ...
    winget install --id Python.Python.3.12 --exact --scope user --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
    call :probe
)
if defined PY goto :done

rem ---------- level 4 : official installer, silent ----------
echo   [3/3] downloading the official Python installer ...
call :download
if not exist "%TEMP%\luye-python-setup.exe" goto :failed
echo         installing silently, please wait ...
"%TEMP%\luye-python-setup.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_pip=1 Include_test=0 SimpleInstall=1
del /q "%TEMP%\luye-python-setup.exe" >nul 2>nul
call :probe
if defined PY goto :done
goto :failed


:done
rem success -- hand the interpreter back to the caller
exit /b 0


rem ------------------------------------------------------------
rem  :probe  --  find a python.exe that exists RIGHT NOW.
rem  Needed because a fresh install does NOT refresh the PATH
rem  of the cmd window we are already running in.
rem ------------------------------------------------------------
:probe
set "PY="
if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" -3"
if defined PY goto :eof
call :tryexe "%LOCALAPPDATA%\Programs\Python"
if defined PY goto :eof
call :tryexe "%ProgramFiles%\Python"
if defined PY goto :eof
call :tryexe "%ProgramFiles(x86)%\Python"
if defined PY goto :eof
py -3 -V >nul 2>nul && set "PY=py -3"
if not defined PY (
    python -V >nul 2>nul && set "PY=python"
)
goto :eof

:tryexe
set "CAND=%~1"
for %%V in (314 313 312 311 310 39) do (
    if not defined PY if exist "%CAND%\Python%%V\python.exe" set "PY="%CAND%\Python%%V\python.exe""
)
goto :eof


rem ------------------------------------------------------------
rem  :download  --  two mirrors, first one that works wins
rem ------------------------------------------------------------
:download
set "DL=%TEMP%\luye-python-setup.exe"
del /q "%DL%" >nul 2>nul
call :tryurl "https://mirrors.huaweicloud.com/python/3.12.10/python-3.12.10-amd64.exe"
if exist "%DL%" goto :eof
call :tryurl "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
goto :eof

:tryurl
echo         from %~1
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;try{Invoke-WebRequest -Uri '%~1' -OutFile '%TEMP%\luye-python-setup.exe' -UseBasicParsing -TimeoutSec 900}catch{exit 1}"
if exist "%TEMP%\luye-python-setup.exe" for %%F in ("%TEMP%\luye-python-setup.exe") do if %%~zF LSS 10000000 del /q "%TEMP%\luye-python-setup.exe" >nul 2>nul
goto :eof


rem ------------------------------------------------------------
:failed
echo.
echo  ------------------------------------------------------------------
echo   [X] Could not set up Python automatically.
echo       Installing it by hand is a 3-minute job:
echo         1. open  https://www.python.org/downloads/
echo         2. download the Windows installer and run it
echo         3. on the FIRST screen, TICK  "Add python.exe to PATH"
echo         4. double-click the installer bat in this folder again
echo       Stuck? Send a photo of this window to Lao Da.
echo  ------------------------------------------------------------------
echo.
pause
exit /b 1
