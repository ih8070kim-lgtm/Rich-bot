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


# ★ V10.31c: status_server 자동 기동 (대시보드 HTTP :7777)
# 이전엔 SSH로 수동 실행해야 했음 → main.py와 함께 자동 시작
def _status_server_loop():
    import subprocess
    import sys
    ss_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "status_server.py")
    if not os.path.exists(ss_path):
        print(f"[status_server] 파일 없음: {ss_path}")
        return
    # 무한 재시작 루프 (크래시 나도 10초 후 재기동)
    while True:
        try:
            print(f"[status_server] 기동 → http://<EC2_IP>:7777/")
            proc = subprocess.Popen(
                [sys.executable, ss_path, "7777"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            proc.wait()
            print(f"[status_server] 종료됨 (rc={proc.returncode}), 10초 후 재기동")
        except Exception as _e:
            print(f"[status_server] 기동 실패: {_e}")
        time.sleep(10)

threading.Thread(target=_status_server_loop, daemon=True).start()


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
