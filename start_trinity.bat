@echo off
cd /d C:\Users\김익현\OneDrive\Desktop\부자만들기

echo [START] TRINITY SYSTEM

start "TRINITY_WATCHDOG" cmd /k python watchdog.py
timeout /t 2 > nul
start "TRINITY_TELEGRAM" cmd /k python telegram_bot.py
timeout /t 2 > nul
start "TRINITY_DEPLOY" cmd /k python telegram_deploy_bot.py