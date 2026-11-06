@echo off
setlocal
for %%I in ("%~dp0.") do set "PIPELINE_ROOT=%%~fI"
cd /d "%PIPELINE_ROOT%"

set "PINOKIO_PY=C:\Users\bugbl\Downloads\pinokio\api\wan.git\app\venv\Scripts\python.exe"
if not exist "%PINOKIO_PY%" set "PINOKIO_PY=python"

echo ============================================================================
echo   Music Video Generation Studio - Web UI Launcher
echo ============================================================================
echo   Server URL : http://127.0.0.1:7860
echo   Root Dir   : %PIPELINE_ROOT%
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

