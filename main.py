"""
Trinity Main Entry Point
RUN_MODE = "V8"  → 기존 V8 엔진 (StrategyC 루프) 실행
RUN_MODE = "V9"  → V9 실험 엔진 실행
"""

import os

print("RUNNING FROM:", os.path.abspath(__file__))

RUN_MODE = "V8"   # "V8" or "V9"
V9_DRY_RUN = True


def main_async_loop():
    """✅ 기존 V8 엔진 시작 코드 (StrategyC 루프)"""
    from v9.app.runner import run as _v9_run
    # V8 실제 루프가 복원될 때까지 임시로 V9 runner를 V8 모드로 실행
    # (실제 V8 루프 코드가 있다면 이 함수 내부를 교체하면 됩니다)
    _v9_run(dry_run=False)


if __name__ == "__main__":
    if RUN_MODE == "V9":
        from v9.app.runner import run
        run(dry_run=V9_DRY_RUN)
    else:
        # ✅ 기존 V8 엔진 시작 코드(StrategyC 루프) 그대로 실행
        main_async_loop()
