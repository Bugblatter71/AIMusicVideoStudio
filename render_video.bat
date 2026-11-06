REM ============================================================================
REM   Generic Music Video Render Pipeline - Quick Reference
REM ============================================================================
REM Launch Local Studio Web UI:                       .\launch_ui.bat
REM Create fresh project structure:                   .\render_video.bat --new [my_song]
REM Auto-render edits (detects changes in JSON):       .\render_video.bat all_or_nothing
REM List project catalog (anchors, scenes, models):   .\render_video.bat all_or_nothing --list
REM Switch resolution on the fly (options 1 to 6):     .\render_video.bat all_or_nothing --res 4
REM Switch video model on the fly (options 1 to 16):   .\render_video.bat all_or_nothing --vr 1
REM Regenerate specific anchor(s) ONLY:               .\render_video.bat all_or_nothing --anchors-only A01 A02
REM Regenerate ALL anchors ONLY:                      .\render_video.bat all_or_nothing --anchors-only all-anchors
REM Regenerate specific anchor(s) + affected scenes:  .\render_video.bat all_or_nothing A01 A02
REM Regenerate specific scene(s) + stitch master:     .\render_video.bat all_or_nothing S01 S02
REM Re-render ONLY Pass 2 of a 2-pass scene:          .\render_video.bat all_or_nothing S02:p2
REM Re-render ALL scenes + stitch master:             .\render_video.bat all_or_nothing all
REM Re-stitch master MP4 immediately (0s GPU time):   .\render_video.bat all_or_nothing stitch
REM ============================================================================
@echo off
setlocal
for %%I in ("%~dp0.") do set "PIPELINE_ROOT=%%~fI"
cd /d "%PIPELINE_ROOT%"

set "PINOKIO_PY=C:\Users\bugbl\Downloads\pinokio\api\wan.git\app\venv\Scripts\python.exe"
if not exist "%PINOKIO_PY%" set "PINOKIO_PY=python"
set "PATH=C:\Users\bugbl\Downloads\pinokio\bin;C:\Users\bugbl\Downloads\pinokio\bin\miniconda\Library\bin;C:\Users\bugbl\Downloads\pinokio\api\wan.git\app\ffmpeg_bins;%PATH%"
set "GIT_PYTHON_REFRESH=quiet"

echo ============================================================================
echo   Generic Music Video Render Pipeline
echo ============================================================================
echo   Root   : "%PIPELINE_ROOT%"
echo   Runner : "%PIPELINE_ROOT%\work\run_overnight_v3.py"
echo   Args   : %*
echo ============================================================================

"%PINOKIO_PY%" -u "%PIPELINE_ROOT%\work\run_overnight_v3.py" %*
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXIT_CODE%"=="0" (
    echo [ERROR] Pipeline exited with code %EXIT_CODE%.
    pause
    exit /b %EXIT_CODE%
)

REM If launched by double-clicking in Explorer, pause so the window stays open
echo %CMDCMDLINE% | findstr /i /c:"/c" >nul
if %ERRORLEVEL%==0 (
    echo.
    pause
)
exit /b 0
