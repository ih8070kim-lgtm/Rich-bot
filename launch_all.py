import os
import subprocess
from pathlib import Path

BASE = Path(r"C:\Users\김익현\OneDrive\Desktop\부자만들기").resolve()

def launch(title: str, script: str) -> None:
    subprocess.Popen(
        f'start "{title}" cmd /k python "{script}"',
        cwd=str(BASE),
        shell=True,
    )

if __name__ == "__main__":
    launch("TRINITY_WATCHDOG", "watchdog.py")
    launch("TRINITY_TELEGRAM", "telegram_bot.py")
    launch("TRINITY_DEPLOY", "telegram_deploy_bot.py")