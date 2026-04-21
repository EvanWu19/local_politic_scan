@echo off
echo ── Local Politics Scanner Setup ──────────────────────────
echo.

REM Create virtual environment
python -m venv .venv
if errorlevel 1 (
    echo ERROR: Python venv creation failed. Make sure Python 3.10+ is installed.
    pause
    exit /b 1
)

REM Activate and install dependencies
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

REM Create .env from example if it doesn't exist
if not exist .env (
    copy .env.example .env
    echo.
    echo IMPORTANT: Edit .env and add your API keys before running scans.
    echo   - ANTHROPIC_API_KEY  (required for AI summaries)
    echo   - CONGRESS_API_KEY   (required for federal bills)
    echo   - OPENSTATES_API_KEY (optional, for MD state bills)
    echo.
)

REM Create data and reports directories
mkdir data 2>nul
mkdir reports 2>nul

echo.
echo ✓ Installation complete!
echo.
echo Next steps:
echo   1. Edit .env and add your API keys
echo   2. Run your first scan:  .venv\Scripts\python main.py scan
echo   3. Schedule daily runs:  .venv\Scripts\python main.py setup
echo.
pause
