@echo off
setlocal
for %%I in ("%~dp0.") do set "PIPELINE_ROOT=%%~fI"
cd /d "%PIPELINE_ROOT%"

REM Load machine-local config if present
if exist "%PIPELINE_ROOT%\config.local.bat" call "%PIPELINE_ROOT%\config.local.bat"

REM Auto-detect Pinokio Wan2GP Python environment if not already set
if not defined PINOKIO_PY (
    if exist "%USERPROFILE%\pinokio\api\wan.git\app\venv\Scripts\python.exe" (
        set "PINOKIO_PY=%USERPROFILE%\pinokio\api\wan.git\app\venv\Scripts\python.exe"
    ) else if exist "%USERPROFILE%\Downloads\pinokio\api\wan.git\app\venv\Scripts\python.exe" (
        set "PINOKIO_PY=%USERPROFILE%\Downloads\pinokio\api\wan.git\app\venv\Scripts\python.exe"
    ) else if exist "%USERPROFILE%\Documents\pinokio\api\wan.git\app\venv\Scripts\python.exe" (
        set "PINOKIO_PY=%USERPROFILE%\Documents\pinokio\api\wan.git\app\venv\Scripts\python.exe"
    ) else if exist "C:\pinokio\api\wan.git\app\venv\Scripts\python.exe" (
        set "PINOKIO_PY=C:\pinokio\api\wan.git\app\venv\Scripts\python.exe"
    ) else if exist "D:\pinokio\api\wan.git\app\venv\Scripts\python.exe" (
        set "PINOKIO_PY=D:\pinokio\api\wan.git\app\venv\Scripts\python.exe"
    ) else (
        set "PINOKIO_PY=python"
    )
)

echo ============================================================================
echo   Music Video Generation Studio - Web UI Launcher
echo ============================================================================
echo   Server URL : http://127.0.0.1:7860
echo   Root Dir   : %PIPELINE_ROOT%
echo   Python Env : %PINOKIO_PY%
echo ============================================================================
echo.
echo Launching your browser at http://127.0.0.1:7860 ...
start "" cmd /c "timeout /t 1 /nobreak >nul && start http://127.0.0.1:7860"

"%PINOKIO_PY%" -u "%PIPELINE_ROOT%\work\ui\server.py" --port 7860
if not "%ERRORLEVEL%"=="0" (
    echo.
    echo [ERROR] UI Server exited with code %ERRORLEVEL%.
    pause
)
