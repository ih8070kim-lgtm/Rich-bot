"""
V9 Utils - Time

★ V10.31AK: 시스템 내 모든 타임스탬프 UTC 통일
- Binance/ccxt 전부 UTC — 거래 체결 시각과 로그 시각 정합성
- 표시 레이어(텔레그램 등)에서만 KST로 변환
"""
import time
from datetime import datetime, timezone


def now_ts() -> float:
    """현재 Unix timestamp (float) — UTC 무관 (epoch 초)"""
    return time.time()


def now_str() -> str:
    """현재 시각 문자열 (UTC, ISO-like)"""
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def elapsed_sec(since_ts: float) -> float:
    """since_ts 이후 경과 초"""
    return time.time() - since_ts


def today_str() -> str:
    """오늘 날짜 문자열 YYYY-MM-DD (UTC 기준)"""
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')
