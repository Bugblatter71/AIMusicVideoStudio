@echo off
setlocal
for %%I in ("%~dp0.") do set "PIPELINE_ROOT=%%~fI"
cd /d "%PIPELINE_ROOT%"

set "STALE_CFG=0"
REM Load machine-local config if present
if exist "%PIPELINE_ROOT%\config.local.bat" call "%PIPELINE_ROOT%\config.local.bat"

REM Invalidate stale paths if a previous Pinokio / Wan2GP install was moved or deleted
if defined PINOKIO_PY (
    if /I "%PINOKIO_PY%"=="python" (
        set "PINOKIO_PY="
    ) else if not exist "%PINOKIO_PY%" (
        echo [WARN] Configured PINOKIO_PY no longer exists: %PINOKIO_PY%
        echo        Re-scanning Pinokio / Wan2GP paths...
        set "PINOKIO_PY="
        set "STALE_CFG=1"
    )
)
set "_WGP_OK=0"
if defined WGP_ROOT if exist "%WGP_ROOT%\wgp_config.json" set "_WGP_OK=1"
if defined WGP_ROOT if exist "%WGP_ROOT%\wgp.py" set "_WGP_OK=1"
if defined WGP_ROOT if exist "%WGP_ROOT%\models" set "_WGP_OK=1"
if defined WGP_ROOT if exist "%WGP_ROOT%\shared\api.py" set "_WGP_OK=1"
if defined WGP_ROOT if "%_WGP_OK%"=="0" (
    echo [WARN] Configured WGP_ROOT is missing or invalid: %WGP_ROOT%
    set "WGP_ROOT="
    set "STALE_CFG=1"
)

REM Auto-detect Pinokio Wan2GP Python environment if unset or cleared above
if not defined PINOKIO_PY if defined WGP_PYTHON if exist "%WGP_PYTHON%" set "PINOKIO_PY=%WGP_PYTHON%"
if not defined PINOKIO_PY if defined ENGINE_PYTHON if exist "%ENGINE_PYTHON%" set "PINOKIO_PY=%ENGINE_PYTHON%"
if not defined PINOKIO_PY if defined WGP_ROOT if exist "%WGP_ROOT%\venv\Scripts\python.exe" set "PINOKIO_PY=%WGP_ROOT%\venv\Scripts\python.exe"

if not defined PINOKIO_PY if defined PINOKIO_HOME if exist "%PINOKIO_HOME%\api\wan.git\app\venv\Scripts\python.exe" set "PINOKIO_PY=%PINOKIO_HOME%\api\wan.git\app\venv\Scripts\python.exe"
if not defined PINOKIO_PY if defined PINOKIO_ROOT if exist "%PINOKIO_ROOT%\api\wan.git\app\venv\Scripts\python.exe" set "PINOKIO_PY=%PINOKIO_ROOT%\api\wan.git\app\venv\Scripts\python.exe"

if not defined PINOKIO_PY (
    for %%P in (
        "%USERPROFILE%\pinokio\api\wan.git\app\venv\Scripts\python.exe"
        "%USERPROFILE%\Downloads\pinokio\api\wan.git\app\venv\Scripts\python.exe"
        "%USERPROFILE%\Documents\pinokio\api\wan.git\app\venv\Scripts\python.exe"
        "%LOCALAPPDATA%\Programs\pinokio\api\wan.git\app\venv\Scripts\python.exe"
    ) do (
        if not defined PINOKIO_PY if exist "%%~P" set "PINOKIO_PY=%%~P"
    )
)
if not defined PINOKIO_PY (
    for %%D in (C D E F G H I J) do (
        if not defined PINOKIO_PY if exist "%%D:\pinokio\api\wan.git\app\venv\Scripts\python.exe" (
            set "PINOKIO_PY=%%D:\pinokio\api\wan.git\app\venv\Scripts\python.exe"
        )
    )
)
if not defined PINOKIO_PY (
    for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[System.IO.DriveInfo]::GetDrives() | Where-Object { $_.DriveType -eq 'Fixed' -and $_.IsReady } | ForEach-Object { $_.Name.Substring(0,1) }" 2^>nul`) do (
        if not defined PINOKIO_PY if exist "%%D:\pinokio\api\wan.git\app\venv\Scripts\python.exe" (
            set "PINOKIO_PY=%%D:\pinokio\api\wan.git\app\venv\Scripts\python.exe"
        )
    )
)
if not defined PINOKIO_PY (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PINOKIO_PY set "PINOKIO_PY=%%~fP"
    )
)

REM Align WGP_ROOT, PATH, and heal stale config.local.bat / local_config.json when relocated
if defined PINOKIO_PY (
    for %%A in ("%PINOKIO_PY%\..\..\..") do if not defined WGP_ROOT (
        if exist "%%~fA\wgp_config.json" set "WGP_ROOT=%%~fA"
        if not defined WGP_ROOT if exist "%%~fA\models" set "WGP_ROOT=%%~fA"
        if not defined WGP_ROOT if exist "%%~fA\wgp.py" set "WGP_ROOT=%%~fA"
        if not defined WGP_ROOT if exist "%%~fA\shared\api.py" set "WGP_ROOT=%%~fA"
    )
)
if defined WGP_ROOT (
    for %%B in ("%WGP_ROOT%\..\..\..") do (
        set "PATH=%%~fB\bin;%%~fB\bin\miniconda\Library\bin;%WGP_ROOT%\ffmpeg_bins;%PATH%"
    )
    if "%STALE_CFG%"=="1" if exist "%PIPELINE_ROOT%\config.local.bat" if defined PINOKIO_PY (
        for %%B in ("%WGP_ROOT%\..\..\..") do (
            > "%PIPELINE_ROOT%\config.local.bat" echo @echo off
            >> "%PIPELINE_ROOT%\config.local.bat" echo REM Machine-local overrides for Music Video Studio (ignored by git)
            >> "%PIPELINE_ROOT%\config.local.bat" echo set "PINOKIO_PY=%PINOKIO_PY%"
            >> "%PIPELINE_ROOT%\config.local.bat" echo set "WGP_ROOT=%WGP_ROOT%"
            >> "%PIPELINE_ROOT%\config.local.bat" echo set "PATH=%%~fB\bin;%%~fB\bin\miniconda\Library\bin;%WGP_ROOT%\ffmpeg_bins;%%PATH%%"
        )
        echo [OK] Updated stale config.local.bat -> %WGP_ROOT%
    )
)

if not defined PINOKIO_PY (
    echo [ERROR] Could not locate any working Python or Pinokio Wan2GP environment.
    pause
    exit /b 3
)

echo ============================================================================
echo   Music Video Generation Studio - Web UI Launcher
echo ============================================================================
echo   Server URL : http://127.0.0.1:7860
echo   Root Dir   : %PIPELINE_ROOT%
echo   Python Env : %PINOKIO_PY%
echo ============================================================================
echo.
if not "%MVG_NO_BROWSER%"=="1" (
    echo Launching your browser at http://127.0.0.1:7860 ...
    start "" cmd /c "timeout /t 1 /nobreak >nul && start http://127.0.0.1:7860"
)

"%PINOKIO_PY%" -u "%PIPELINE_ROOT%\work\ui\server.py" --port 7860
if not "%ERRORLEVEL%"=="0" (
    echo.
    echo [ERROR] UI Server exited with code %ERRORLEVEL%.
    pause
)
