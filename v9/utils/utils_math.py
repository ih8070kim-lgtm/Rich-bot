"""
V9 Utils - Math
수학/통계 유틸리티
"""

import numpy as np


def safe_float(val, default: float = 0.0) -> float:
    """안전한 float 변환"""
    try:
        return float(val) if val is not None else default
    except Exception:
        return default


def safe_corr(btc_lr: np.ndarray, alt_lr: np.ndarray) -> float:
    """
    BTC log-return과 ALT log-return의 피어슨 상관계수 (안전 버전)
    길이 불일치 시 짧은 쪽에 맞춤. NaN/Inf 포함 시 0.0 반환.
    """
    try:
        min_len = min(len(btc_lr), len(alt_lr))
        if min_len < 5:
            return 0.0
        b = btc_lr[-min_len:]
        a = alt_lr[-min_len:]
        mask = np.isfinite(b) & np.isfinite(a)
        b, a = b[mask], a[mask]
        if len(b) < 5:
            return 0.0
        std_b = np.std(b)
        std_a = np.std(a)
        if std_b < 1e-10 or std_a < 1e-10:
            return 0.0
        corr = np.corrcoef(b, a)[0, 1]
        if np.isnan(corr) or np.isinf(corr):
            return 0.0
        return float(corr)
    except Exception:
        return 0.0



def atr_from_ohlcv(ohlcv: list, period: int = 10) -> float:
    """
    ATR(period) 계산. ohlcv: [[ts, open, high, low, close, vol], ...]
    """
    try:
        if len(ohlcv) < period + 1:
            return 0.0
        tr_list = []
        for i in range(1, len(ohlcv)):
            h = float(ohlcv[i][2])
            l = float(ohlcv[i][3])
            prev_c = float(ohlcv[i - 1][4])
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            tr_list.append(tr)
        if len(tr_list) < period:
            return 0.0
        return float(np.mean(tr_list[-period:]))
    except Exception:
        return 0.0


def calc_rsi(closes: list[float], period: int = 14) -> float:
    """RSI 계산 — Wilder's Smoothed Moving Average (거래소 표준)"""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(diff))
    # [FIX] 단순평균 → Wilder's Smoothed MA
    # 1단계: 첫 period 구간 SMA로 초기값 설정
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # 2단계: 이후 지수평활 (Wilder's: 가중치 = 1/period)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))


def calc_ema(closes: list[float], period: int = 20) -> float:
    """EMA 계산 — 초기값: 첫 period개의 SMA (표준 방식)"""
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    # [FIX] closes[0] 단일값 시드 → 수렴까지 왜곡 발생
    # 표준: 첫 period개의 SMA로 시드값 설정
    ema = sum(closes[:period]) / period
    alpha = 2 / (period + 1)
    for c in closes[period:]:
        ema = (c - ema) * alpha + ema
    return ema


def log_returns(prices: np.ndarray) -> np.ndarray:
    """log-return 계산: np.diff(np.log(prices))"""
    return np.diff(np.log(prices))


def calc_roi_pct(ep: float, cp: float, side: str, leverage: float) -> float:
    """
    레버리지 반영 ROI를 퍼센트(%) 단위로 반환 (수수료 미반영 gross).
    반환값 예시: -7.5, +1.2, -15.0
    용도: DCA 트리거, 헷지 트리거 (포지션 방어 결정)
    """
    if ep <= 0 or cp <= 0:
        return 0.0
    raw = (cp - ep) / ep if side == "buy" else (ep - cp) / ep
    return raw * leverage * 100.0


def calc_roi_pct_net(ep: float, cp: float, side: str, leverage: float,
                     fee_rate: float = 0.0008) -> float:
    """
    레버리지 반영 ROI를 퍼센트(%) 단위로 반환 (수수료 반영 net).
    왕복 수수료 = (ep + cp) * fee_rate / ep * leverage * 100
    용도: TP1 트리거, trailing 익절 판단 (실손익 기준)

    예시: ep=100, cp=100.5, side=buy, lev=3, fee=0.0008
      gross = +1.4%
      fee_pct = (100+100.5)*0.0008/100 * 3 * 100 ≈ 0.449%
      net = +1.4 - 0.449 ≈ +0.95%
    """
    if ep <= 0 or cp <= 0:
        return 0.0
    raw = (cp - ep) / ep if side == "buy" else (ep - cp) / ep
    gross_pct = raw * leverage * 100.0
    # 왕복 수수료 (진입가 기준 정규화)
    fee_pct = (ep + cp) * fee_rate / ep * leverage * 100.0
    return gross_pct - fee_pct


# ═══════════════════════════════════════════════════════════════════
# ★ V10.31AI: role 기반 레버리지 자동 처리
# ═══════════════════════════════════════════════════════════════════
# BC(Beta Cycle), CB(Crash Bounce)는 x1 레버리지 독립 전략. 나머지는 LEVERAGE(=3).
# 기존 calc_roi_pct(..., LEVERAGE) 호출부 중 BC/CB가 거치는 경로에서만 사용.
# MR/HEDGE 전용 경로(planners heavy_rois, hedge_engine 등)는 기존 그대로 유지.

def role_leverage(role: str) -> int:
    """role 기반 실제 레버리지 반환. BC/CB=1, 나머지=LEVERAGE(3)."""
    from v9.config import LEVERAGE
    return 1 if role in ("BC", "CB") else int(LEVERAGE)


def calc_roi_pct_by_role(ep: float, cp: float, side: str, role: str) -> float:
    """role 기반 레버리지 자동 적용 ROI 계산 (gross, 수수료 미포함).
    
    BC/CB 포지션은 x1 레버리지로 실제 체결되므로 ROI도 x1 기준이어야
    대시보드/로그 표시가 실제 PnL%와 일치.
    """
    return calc_roi_pct(ep, cp, side, role_leverage(role))

