@echo off
cd /d "%~dp0"

rem venv + зависимости только при первом запуске
if not exist venv (
    python -m venv venv
    call venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)

:loop
python repair_worker.py
echo [run.bat] worker exited, restart in 10s
timeout /t 10 /nobreak >nul
goto loop
