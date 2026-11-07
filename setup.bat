@echo off
setlocal enabledelayedexpansion
for %%I in ("%~dp0.") do set "PIPELINE_ROOT=%%~fI"
cd /d "%PIPELINE_ROOT%"

echo ============================================================================
echo   Music Video Generation Studio - First-Time Setup Wizard
echo ============================================================================
echo   This wizard will detect your local Pinokio Wan2GP installation,
echo   configure local paths, verify FFmpeg, and get your studio ready.
echo ============================================================================
echo.

REM 1. Check Git
where git >nul 2>nul
if %ERRORLEVEL%==0 (
    echo [OK] Git is installed.
) else (
    echo [WARN] Git command-line tool was not found in PATH.
)

REM 2. Look for existing config.local.bat
if exist "%PIPELINE_ROOT%\config.local.bat" (
    echo [INFO] Existing config.local.bat found.
)

REM 3. Search for Pinokio Wan2GP
echo.
echo Searching for Pinokio Wan2GP installation...
set "DETECTED_WGP="

if exist "%USERPROFILE%\pinokio\api\wan.git\app\wgp_config.json" (
    set "DETECTED_WGP=%USERPROFILE%\pinokio\api\wan.git\app"
) else if exist "%USERPROFILE%\Downloads\pinokio\api\wan.git\app\wgp_config.json" (
    set "DETECTED_WGP=%USERPROFILE%\Downloads\pinokio\api\wan.git\app"
) else if exist "%USERPROFILE%\Documents\pinokio\api\wan.git\app\wgp_config.json" (
    set "DETECTED_WGP=%USERPROFILE%\Documents\pinokio\api\wan.git\app"
) else if exist "C:\pinokio\api\wan.git\app\wgp_config.json" (
    set "DETECTED_WGP=C:\pinokio\api\wan.git\app"
) else if exist "D:\pinokio\api\wan.git\app\wgp_config.json" (
    set "DETECTED_WGP=D:\pinokio\api\wan.git\app"
) else if exist "E:\pinokio\api\wan.git\app\wgp_config.json" (
    set "DETECTED_WGP=E:\pinokio\api\wan.git\app"
)

set "CHOSEN_WGP="
if defined DETECTED_WGP (
    echo [OK] Auto-detected Wan2GP at: !DETECTED_WGP!
    set /p "USE_AUTO=Use this Wan2GP installation? [Y/n]: "
    if "!USE_AUTO!"=="" set "USE_AUTO=y"
    if /i "!USE_AUTO!"=="y" (
        set "CHOSEN_WGP=!DETECTED_WGP!"
    )
)

if not defined CHOSEN_WGP (
    echo.
    echo If Pinokio is installed elsewhere, please paste the full path to
    echo either the Pinokio root folder (e.g. C:\pinokio) or the Wan2GP app folder
    echo (e.g. C:\pinokio\api\wan.git\app).
    echo.
    set /p "USER_INPUT_PATH=Path (or press Enter to skip GPU auto-hook): "
    if defined USER_INPUT_PATH (
        REM Strip quotes
        set "USER_INPUT_PATH=!USER_INPUT_PATH:"=!"
        if exist "!USER_INPUT_PATH!\api\wan.git\app\wgp_config.json" (
            set "CHOSEN_WGP=!USER_INPUT_PATH!\api\wan.git\app"
        ) else if exist "!USER_INPUT_PATH!\wgp_config.json" (
            set "CHOSEN_WGP=!USER_INPUT_PATH!"
        ) else if exist "!USER_INPUT_PATH!\venv\Scripts\python.exe" (
            set "CHOSEN_WGP=!USER_INPUT_PATH!"
        ) else (
            echo [WARN] Could not find Wan2GP inside "!USER_INPUT_PATH!".
        )
    )
)

REM 4. Generate local configuration
if defined CHOSEN_WGP (
    echo.
    echo Writing local configuration overrides...
    set "PIN_ROOT=!CHOSEN_WGP:\api\wan.git\app=!"
    set "WGP_PY=!CHOSEN_WGP!\venv\Scripts\python.exe"

    (
        echo @echo off
        echo REM Machine-local overrides for Music Video Studio ^(ignored by git^)
        echo set "PINOKIO_PY=!WGP_PY!"
        echo set "WGP_ROOT=!CHOSEN_WGP!"
        echo set "PATH=!PIN_ROOT!\bin;!PIN_ROOT!\bin\miniconda\Library\bin;!CHOSEN_WGP!\ffmpeg_bins;%%PATH%%"
    ) > "%PIPELINE_ROOT%\config.local.bat"
    echo [OK] Created config.local.bat

    REM Generate JSON equivalent for Python
    powershell -Command "$wgp = $env:CHOSEN_WGP.Replace('\', '/'); $root = $env:PIN_ROOT.Replace('\', '/'); $py = $env:WGP_PY.Replace('\', '/'); @{ wgp_root = $wgp; pinokio_root = $root; python_path = $py } | ConvertTo-Json | Set-Content local_config.json"
    echo [OK] Created local_config.json
) else (
    echo.
    echo [INFO] Continuing without custom Pinokio Wan2GP path.
    echo System python will be used for Studio UI.
)

REM 5. Verify FFmpeg
echo.
echo Checking FFmpeg availability...
if not exist "%PIPELINE_ROOT%\tools" mkdir "%PIPELINE_ROOT%\tools"

if exist "%PIPELINE_ROOT%\tools\ffmpeg.exe" (
    echo [OK] Found tools\ffmpeg.exe
) else if defined CHOSEN_WGP and exist "!CHOSEN_WGP!\ffmpeg_bins\ffmpeg.exe" (
    echo [INFO] Copying FFmpeg from Wan2GP bundle into tools\ffmpeg.exe...
    copy /y "!CHOSEN_WGP!\ffmpeg_bins\ffmpeg.exe" "%PIPELINE_ROOT%\tools\ffmpeg.exe" >nul
    if exist "%PIPELINE_ROOT%\tools\ffmpeg.exe" (
        echo [OK] tools\ffmpeg.exe installed successfully.
    )
) else (
    where ffmpeg >nul 2>nul
    if %ERRORLEVEL%==0 (
        echo [OK] Found system ffmpeg in PATH.
    ) else (
        echo [WARN] FFmpeg was not detected. Video stitching requires FFmpeg.
        echo You can download ffmpeg and place it in the 'tools\' folder.
    )
)

REM 6. Run verification
echo.
echo ============================================================================
echo   Running Quick Self-Test
echo ============================================================================
if exist "%PIPELINE_ROOT%\config.local.bat" call "%PIPELINE_ROOT%\config.local.bat"
if not defined PINOKIO_PY set "PINOKIO_PY=python"

"%PINOKIO_PY%" -m unittest tests/test_backend_queue.py
if %ERRORLEVEL%==0 (
    echo.
    echo ============================================================================
    echo   [SUCCESS] All systems verified! Music Video Studio is ready to use.
    echo ============================================================================
) else (
    echo.
    echo [WARN] Verification tests exited with status %ERRORLEVEL%.
)

echo.
set /p "LAUNCH_NOW=Would you like to launch the Studio Web UI now? [Y/n]: "
if "!LAUNCH_NOW!"=="" set "LAUNCH_NOW=y"
if /i "!LAUNCH_NOW!"=="y" (
    start "" "%PIPELINE_ROOT%\launch_ui.bat"
)

echo.
echo Setup completed. You can launch anytime by double-clicking launch_ui.bat.
pause
