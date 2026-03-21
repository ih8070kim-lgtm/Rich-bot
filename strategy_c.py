"""
strategy_c.py — 호환용 Shim (V9 전환 후)

[LOCK] 이 파일은 레거시 호환성을 위한 껍데기(shim)입니다.
실제 전략 실행은 v9/app/runner.py (V9 Runner)가 담당합니다.
이 파일의 내부 로직(DCA/TP/슬롯 등)은 수정하지 않습니다.
main.py → V9 runner 전환 후 이 파일은 import되지 않습니다.
"""

# ── 레거시 import 유지 (다른 모듈이 참조할 경우 대비) ──────────
import json
import time
from datetime import datetime

try:
    from telegram_engine import send_telegram_message
except ImportError:

    async def send_telegram_message(msg):
        pass


_SC_EVENT_LOG = "log_events.jsonl"


def _sc_append_event(event_type: str, payload: dict):
    try:
        record = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": event_type,
            **payload,
        }
        with open(_SC_EVENT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


class StrategyC:
    """
    [SHIM] V9 전환 후 레거시 호환용 껍데기.
    실제 거래 로직은 실행되지 않습니다.
    main.py가 V9 runner를 직접 호출하므로 이 클래스는 사용되지 않습니다.
    """

    def __init__(self, exchange=None, shared_data=None, main_executor=None, ai_engine=None):
        self.ex = exchange
        self.shared = shared_data or {}
        self.execute_order = main_executor
        self.ai = ai_engine
        self.tag = "STRAT_C_SHIM_V9"
        self.st = {}
        self.cooldowns = {}
        self.allocated_capital = 0.0
        self.leverage = 3
        self.max_long_slots = 4
        self.max_short_slots = 4
        self.total_max_slots = 8
        self.long_symbols = []
        self.short_symbols = []
        self.target_symbols = []
        self.regime = "MID"
        self.start_time = time.time()
        print(f"[{self.tag}] Shim 초기화 완료 — 실제 실행은 V9 Runner가 담당합니다.")

    async def update(self, shared_data=None):
        """[SHIM] 실제 로직 없음 — V9 Runner가 담당"""
        pass

    @property
    def slots(self):
        return []
