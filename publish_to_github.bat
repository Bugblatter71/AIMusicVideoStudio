@echo off
setlocal enabledelayedexpansion
for %%I in ("%~dp0.") do set "PIPELINE_ROOT=%%~fI"
cd /d "%PIPELINE_ROOT%"

echo ============================================================================
echo   Music Video Generation Studio - Publish to GitHub
echo ============================================================================
echo   This script will help you push your local repository to a public
echo   GitHub repository.
echo ============================================================================
echo.

where git >nul 2>nul
if not %ERRORLEVEL%==0 (
    echo [ERROR] Git was not found in your PATH.
    echo Please install Git from https://git-scm.com/download/win and try again.
    pause
    exit /b 1
)

REM Rename default branch to main
git branch -M main 2>nul

echo Step 1: Create a new repository on GitHub:
echo   1. Visit: https://github.com/new
echo   2. Repository name: MusicVideoGeneration (or any name you prefer)
echo   3. Set to Public
echo   4. IMPORTANT: Leave "Add a README", ".gitignore", and "Choose a license" UNCHECKED!
echo      (This project already contains them)
echo   5. Click "Create repository"
echo.
echo Step 2: Paste the repository URL here:
set /p "REPO_URL=Repository URL (e.g. https://github.com/your-username/MusicVideoGeneration.git): "

if not defined REPO_URL (
    echo [ERROR] No URL provided. Aborting.
    pause
    exit /b 1
)

REM Strip surrounding quotes if present
set "REPO_URL=!REPO_URL:"=!"

echo.
echo Configuring remote 'origin' -> !REPO_URL!...
git remote remove origin 2>nul
git remote add origin "!REPO_URL!"

echo.
echo Pushing 'main' branch to GitHub...
echo (If prompted, log into GitHub in the browser/window that appears)
echo.
git push -u origin main

if %ERRORLEVEL%==0 (
    echo.
    echo ============================================================================
    echo   [SUCCESS] Your repository is now live on GitHub!
    echo   !REPO_URL!
    echo ============================================================================
) else (
    echo.
    echo [ERROR] Git push failed (exit code %ERRORLEVEL%).
    echo Please check your repository URL and GitHub authentication permissions.
)

echo.
pause
