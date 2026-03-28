"""
V9 Utils - Time
시간 관련 유틸리티
"""
import time
from datetime import UTC, datetime


def now_ts() -> float:
    """현재 Unix timestamp (float)"""
    return time.time()


def now_str() -> str:
    """현재 시각 문자열 (로컬)"""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def now_utc_str() -> str:
    """현재 UTC 시각 문자열"""
    return datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')


def elapsed_sec(since_ts: float) -> float:
    """since_ts 이후 경과 초"""
    return time.time() - since_ts


def elapsed_min(since_ts: float) -> float:
    """since_ts 이후 경과 분"""
    return elapsed_sec(since_ts) / 60.0


def is_expired(since_ts: float, timeout_sec: float) -> bool:
    """since_ts 기준 timeout_sec 초과 여부"""
    return elapsed_sec(since_ts) >= timeout_sec


def today_str() -> str:
    """오늘 날짜 문자열 YYYY-MM-DD"""
    return datetime.now().strftime('%Y-%m-%d')
