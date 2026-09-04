@echo off
REM ==========================================================
REM  cv_lyric_context - emotion tagging launcher
REM  Edit the settings below once, then just double-click.
REM  No PowerShell needed.
REM ==========================================================

set "PYTHON=python"
set "API_KEY=PASTE_YOUR_API_KEY_HERE"
set "DB=E:\mai\maibot\data\plugins\org.mai-mai.cv-lyric-context\vcpedia_songs.db"
set "BASE_URL=https://ark.cn-beijing.volces.com/api/v3"
set "MODEL=REPLACE_WITH_YOUR_MODEL_ID"
set "EXTRA="

cd /d "%~dp0"
set "MAIBOT_ANNOTATE_API_KEY=%API_KEY%"

echo DB   : %DB%
echo URL  : %BASE_URL%
echo MODEL: %MODEL%
echo.

echo [1/2] dry-run 3 songs first (nothing will be written)...
"%PYTHON%" annotate_emotions.py --db "%DB%" --base-url "%BASE_URL%" --model "%MODEL%" --limit 3 --dry-run %EXTRA%
if errorlevel 1 goto :end

echo.
set /p GO="Looks good? Run full annotation now? [y/n] "
if /i not "%GO%"=="y" goto :end

echo [2/2] full run... (Ctrl+C is safe, progress resumes next time)
"%PYTHON%" annotate_emotions.py --db "%DB%" --base-url "%BASE_URL%" --model "%MODEL%" %EXTRA%

:end
echo.
echo Done. Press any key to close.
pause >nul
