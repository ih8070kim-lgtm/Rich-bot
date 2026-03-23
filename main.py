"""
Trinity Main Entry Point
RUN_MODE = "V8"  → 기존 V8 엔진 (StrategyC 루프) 실행
RUN_MODE = "V9"  → V9 실험 엔진 실행
"""

import os
import threading
import time

print("RUNNING FROM:", os.path.abspath(__file__))

RUN_MODE = "V8"   # "V8" or "V9"
V9_DRY_RUN = True

# ★ v10.12: Watchdog heartbeat — 30초마다 갱신
def _heartbeat_loop():
    hb_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "heartbeat.txt")
    while True:
        try:
            with open(hb_path, "w") as f:
                f.write(str(time.time()))
        except Exception:
            pass
        time.sleep(30)

threading.Thread(target=_heartbeat_loop, daemon=True).start()


def main_async_loop():
    """✅ 기존 V8 엔진 시작 코드 (StrategyC 루프)"""
    from v9.app.runner import run as _v9_run
    _v9_run(dry_run=False)


if __name__ == "__main__":
    if RUN_MODE == "V9":
        from v9.app.runner import run
        run(dry_run=V9_DRY_RUN)
    else:
        main_async_loop()
