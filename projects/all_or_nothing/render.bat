@echo off
setlocal
cd /d "%~dp0"
for %%I in ("%~dp0..\..") do set "ROOT_DIR=%%~fI"
call "%ROOT_DIR%\render_video.bat" "%~dp0all_or_nothing_v4.json" %*
exit /b %ERRORLEVEL%
