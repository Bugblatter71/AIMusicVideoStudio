@echo off
setlocal
cd /d "%~dp0"

echo ============================================================================
echo   Git Source Control Initializer (Video Excluded)
echo ============================================================================

where git >nul 2>&1
if not "%ERRORLEVEL%"=="0" (
    echo [ERROR] Git is not installed or not found in PATH.
    echo.
    echo To install Git on Windows:
    echo   winget install --id Git.Git -e --source winget
    echo or download from: https://git-scm.com/download/win
    echo.
    pause
    exit /b 1
)

if not exist ".git" (
    echo Initializing fresh Git repository...
    git init
    git branch -M main
) else (
    echo Existing Git repository found.
)

echo.
echo Staging clean source files (ignoring all MP4s and render caches via .gitignore)...
git add .gitignore
git add *.json *.bat *.md *.txt
git add work/*.py
git add projects/*/
echo.
git status --short
echo.
echo ============================================================================
echo Ready to commit! Run:
echo   git commit -m "Initial commit of music video generation pipeline"
echo ============================================================================
pause

