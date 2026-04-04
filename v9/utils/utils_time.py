"""
V9 Utils - Time
시간 관련 유틸리티
"""
import time
from datetime import datetime


def now_ts() -> float:
    """현재 Unix timestamp (float)"""
    return time.time()


def now_str() -> str:
    """현재 시각 문자열 (로컬)"""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def elapsed_sec(since_ts: float) -> float:
    """since_ts 이후 경과 초"""
    return time.time() - since_ts


def today_str() -> str:
    """오늘 날짜 문자열 YYYY-MM-DD"""
    return datetime.now().strftime('%Y-%m-%d')
