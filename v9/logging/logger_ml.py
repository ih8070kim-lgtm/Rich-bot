"""
V10.9 Trinity — ML Feature Logger
===================================
DCA / FORCE_BALANCE 이벤트 시점 피처 자동 기록.
3개월 후 XGBoost 모델 학습용 데이터셋 생성.

log_ml_features.csv 컬럼:
  time, trace_id, event_type, symbol, side, dca_level,
  regime, ema_pctl, atr_pctl_raw, atr_5m, atr_1m,
  skew, skew_side, rsi_5m, rsi_1m,
  btc_ret_5m, btc_ret_15m, btc_ret_1h,
  curr_roi, max_roi_seen, hold_sec,
  vol_ratio_5m, hour_kst, weekday,
  src_ep, curr_p, ema20_15m, ema20_5m

outcome 컬럼은 이후 log_trades.csv와 trace_id로 조인해서 추가.
"""
import csv
import os
import time
from datetime import datetime, timezone, timedelta

KST = timedelta(hours=9)

_LOG_DIR = None
_LOG_PATH = None
_HEADER_WRITTEN = False


def _ensure_log(log_dir: str):
    global _LOG_DIR, _LOG_PATH, _HEADER_WRITTEN
    if _LOG_DIR == log_dir and _LOG_PATH and os.path.exists(_LOG_PATH):
        return
    _LOG_DIR = log_dir
    os.makedirs(log_dir, exist_ok=True)
    _LOG_PATH = os.path.join(log_dir, "log_ml_features.csv")
    if not os.path.exists(_LOG_PATH):
        _HEADER_WRITTEN = False


_COLUMNS = [
    "time", "trace_id", "event_type", "symbol", "side", "dca_level",
    "regime", "ema_pctl", "atr_pctl_raw", "atr_5m_pct", "atr_1m_pct",
    "skew", "skew_side", "rsi_5m", "rsi_1m",
    "btc_ret_5m", "btc_ret_15m", "btc_ret_1h",
    "curr_roi", "max_roi_seen", "hold_sec",
    "vol_ratio_5m", "hour_kst", "weekday",
    "src_ep", "curr_p", "ema20_15m", "ema20_5m",
]


def log_ml_features(
    trace_id: str,
    event_type: str,       # "DCA_T2", "DCA_T3", "DCA_T4", "DCA_T5", "FORCE_BALANCE"
    symbol: str,
    side: str,
    dca_level: int,
    regime: str,
    ema_pctl: float,
    atr_pctl_raw: float,
    atr_5m_pct: float,
    atr_1m_pct: float,
    skew: float,
    skew_side: str,
    rsi_5m: float,
    rsi_1m: float,
    btc_ret_5m: float,
    btc_ret_15m: float,
    btc_ret_1h: float,
    curr_roi: float,
    max_roi_seen: float,
    hold_sec: float,
    vol_ratio_5m: float,
    src_ep: float,
    curr_p: float,
    ema20_15m: float,
    ema20_5m: float,
    log_dir: str = "v9_logs",
):
    global _HEADER_WRITTEN
    try:
        _ensure_log(log_dir)

        now_utc = datetime.now(timezone.utc)
        now_kst = now_utc + KST
        hour_kst = now_kst.hour
        weekday = now_kst.weekday()  # 0=Mon, 6=Sun

        row = {
            "time": now_utc.strftime("%Y-%m-%d %H:%M:%S"),
            "trace_id": trace_id,
            "event_type": event_type,
            "symbol": symbol,
            "side": side,
            "dca_level": dca_level,
            "regime": regime,
            "ema_pctl": f"{ema_pctl:.4f}",
            "atr_pctl_raw": f"{atr_pctl_raw:.4f}",
            "atr_5m_pct": f"{atr_5m_pct:.6f}",
            "atr_1m_pct": f"{atr_1m_pct:.6f}",
            "skew": f"{skew:.4f}",
            "skew_side": skew_side,
            "rsi_5m": f"{rsi_5m:.1f}",
            "rsi_1m": f"{rsi_1m:.1f}",
            "btc_ret_5m": f"{btc_ret_5m:.6f}",
            "btc_ret_15m": f"{btc_ret_15m:.6f}",
            "btc_ret_1h": f"{btc_ret_1h:.6f}",
            "curr_roi": f"{curr_roi:.2f}",
            "max_roi_seen": f"{max_roi_seen:.2f}",
            "hold_sec": f"{hold_sec:.0f}",
            "vol_ratio_5m": f"{vol_ratio_5m:.2f}",
            "hour_kst": hour_kst,
            "weekday": weekday,
            "src_ep": f"{src_ep:.6f}",
            "curr_p": f"{curr_p:.6f}",
            "ema20_15m": f"{ema20_15m:.6f}",
            "ema20_5m": f"{ema20_5m:.6f}",
        }

        write_header = not _HEADER_WRITTEN and not os.path.exists(_LOG_PATH)
        with open(_LOG_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_COLUMNS)
            if write_header:
                writer.writeheader()
                _HEADER_WRITTEN = True
            elif not _HEADER_WRITTEN:
                _HEADER_WRITTEN = True
            writer.writerow(row)

    except Exception as e:
        print(f"[ML_LOG] 피처 기록 오류(무시): {e}")


def calc_btc_returns(snapshot) -> tuple:
    """BTC 5분/15분/1시간 수익률 계산. Returns: (ret_5m, ret_15m, ret_1h)"""
    try:
        btc_pool = (snapshot.ohlcv_pool or {}).get("BTC/USDT", {})

        ret_5m = 0.0
        ohlcv_5m = btc_pool.get("5m", [])
        if len(ohlcv_5m) >= 2:
            _now = float(ohlcv_5m[-1][4])
            _prev = float(ohlcv_5m[-2][4])
            if _prev > 0:
                ret_5m = (_now - _prev) / _prev

        ret_15m = 0.0
        ohlcv_15m = btc_pool.get("15m", [])
        if len(ohlcv_15m) >= 2:
            _now = float(ohlcv_15m[-1][4])
            _prev = float(ohlcv_15m[-2][4])
            if _prev > 0:
                ret_15m = (_now - _prev) / _prev

        ret_1h = 0.0
        ohlcv_1h = btc_pool.get("1h", [])
        if len(ohlcv_1h) >= 2:
            _now = float(ohlcv_1h[-1][4])
            _prev = float(ohlcv_1h[-2][4])
            if _prev > 0:
                ret_1h = (_now - _prev) / _prev

        return ret_5m, ret_15m, ret_1h
    except Exception:
        return 0.0, 0.0, 0.0


def calc_skew(st: dict, real_bal: float, leverage: int = 3) -> tuple:
    """마진 스큐 계산. Returns: (skew_abs, heavy_side)"""
    try:
        from v9.execution.position_book import iter_positions
        if real_bal <= 0:
            return 0.0, "neutral"
        long_m = short_m = 0.0
        for sym_st in st.values():
            if not isinstance(sym_st, dict):
                continue
            for side, p in iter_positions(sym_st):
                if not isinstance(p, dict):
                    continue
                if p.get("role") in ("HEDGE", "SOFT_HEDGE"):
                    continue
                if p.get("step", 0) >= 1:
                    continue
                notional = float(p.get("amt", 0)) * float(p.get("ep", 0))
                if side == "buy":
                    long_m += notional
                else:
                    short_m += notional
        long_m /= leverage * real_bal
        short_m /= leverage * real_bal
        skew = abs(long_m - short_m)
        heavy = "long" if long_m > short_m else ("short" if short_m > long_m else "neutral")
        return skew, heavy
    except Exception:
        return 0.0, "neutral"


def calc_vol_ratio_5m(ohlcv_5m: list) -> float:
    """5m 거래량 비율 (현재봉 / MA20)"""
    try:
        if len(ohlcv_5m) < 21:
            return 1.0
        vols = [float(x[5]) for x in ohlcv_5m[-21:] if len(x) > 5]
        if len(vols) < 2:
            return 1.0
        vol_now = vols[-1]
        vol_ma = sum(vols[:-1]) / len(vols[:-1])
        return vol_now / vol_ma if vol_ma > 0 else 1.0
    except Exception:
        return 1.0
