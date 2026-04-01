"""
V9 Strategy — Planners  (v10.1 — Pullback 독립 아키텍처)
=========================================================
v9.8 → v10.1 변경:
  [Pullback v10.4]
  - Pullback: MR과 동일 DCA/SL/EXIT 구조 (완전 통합)
  - 진입조건: EMA20_15m < price < EMA20_5m + RSI hook (원복)
  - 트레일링: ATR 구간별 (2~4%→2.0%, 4~6%→1.5%, 6%+→ProfitCap)

  [ASYM v10.4]
  - _plan_open_asymmetric: 슬롯 불균형 대응 (기존 유지)
  - _plan_asym_mr_fail: T2+max_roi=0 트리거 (신규)
    → 알파슬롯(mr<0.60, 비대칭≥30%) 또는 악성재고 킬 후 진입
"""
import math
import time
import uuid
from typing import List, Dict, Optional

from v9.types import Intent, IntentType, MarketSnapshot
from v9.risk.slot_manager import count_slots
from v9.execution.position_book import (
    get_p, set_p, iter_positions, is_active,
    get_pending_entry, set_pending_entry,
)
from v9.engines.hedge_core import (
    calc_skew, plan_hedge_core_entry, plan_hedge_core_manage, is_hedge_dca_blocked,
)



# ═════════════════════════════════════════════════════════════════
# BTC Volatility Regime  (★ v10.5)
# ═════════════════════════════════════════════════════════════════
_regime_ema_pctl = None  # 모듈 레벨 EMA 상태
_regime_last = "NORMAL"  # 마지막 레짐 (히스테리시스용)
_regime_cache_snap_id = None  # ★ v10.9: 틱당 1회 캐싱
_regime_cache_result = "NORMAL"
_high_enter_ts = 0.0  # ★ v10.15: HIGH sticky 진입 시각
_bad_regime_active = False  # ★ V10.17: 좀비킬 전용 BAD 플래그

# ★ V10.17: BAD 레짐 판단 — 좀비킬 전용 (T1 스캘핑 삭제)
BAD_ENTER_THRESH = 0.15   # 좀비킬용 BAD 판정 (pctl 15% 미만)
BAD_EXIT_THRESH  = 0.30   # BAD 해제

def _btc_vol_regime(snapshot: "MarketSnapshot") -> str:
    """
    ★ v10.10: BTC 변동성 레짐 — 멀티 타임프레임 점수제

    3개 타임프레임 ATR 퍼센타일을 가중 합산:
      5m  (40%) — 빠른 감지, 단기 돌파 포착
      15m (35%) — 중기 안정성, 노이즈 필터
      1h  (25%) — 장기 앵커, 레짐 관성 유지

    점수 0~1 → EMA 스무딩 → 히스테리시스 → 레짐 결정
    """
    global _regime_ema_pctl, _regime_last, _regime_cache_snap_id, _regime_cache_result

    _snap_id = id(snapshot)
    if _regime_cache_snap_id == _snap_id:
        return _regime_cache_result

    btc_pool = (snapshot.ohlcv_pool or {}).get("BTC/USDT", {})
    btc_price = float(getattr(snapshot, "btc_price", 0.0) or 0.0)
    if btc_price <= 0:
        return "NORMAL"

    _atr_period = 10
    _atr_need   = _atr_period + 1

    def _calc_atr_pctl(ohlcv: list, lookback: int) -> float:
        """타임프레임별 ATR 퍼센타일 계산."""
        if len(ohlcv) < _atr_need + 10:
            return -1.0  # 데이터 부족
        hist = []
        for i in range(_atr_need, len(ohlcv) + 1):
            chunk = ohlcv[i - _atr_need:i]
            atr_val = atr_from_ohlcv(chunk, period=_atr_period)
            close_p = float(chunk[-1][4])
            if atr_val > 0 and close_p > 0:
                hist.append(atr_val / close_p)
        if len(hist) < 10:
            return -1.0
        window = hist[-lookback:] if len(hist) > lookback else hist
        current = window[-1]
        rank = sum(1 for x in window if x <= current)
        return rank / len(window)

    # ── 3개 타임프레임 퍼센타일 ────────────────────────────
    ohlcv_5m  = btc_pool.get("5m", [])
    ohlcv_15m = btc_pool.get("15m", [])
    ohlcv_1h  = btc_pool.get("1h", [])

    pctl_5m  = _calc_atr_pctl(ohlcv_5m,  150)   # 12.5시간
    pctl_15m = _calc_atr_pctl(ohlcv_15m, 96)    # 24시간
    pctl_1h  = _calc_atr_pctl(ohlcv_1h,  48)    # 48시간

    # ── 가중 합산 (데이터 부족한 TF는 제외) ────────────────
    _weights = []
    _values  = []
    if pctl_5m >= 0:
        _weights.append(0.40);  _values.append(pctl_5m)
    if pctl_15m >= 0:
        _weights.append(0.35);  _values.append(pctl_15m)
    if pctl_1h >= 0:
        _weights.append(0.25);  _values.append(pctl_1h)

    if not _weights:
        return "NORMAL"

    # 가중 평균
    _total_w = sum(_weights)
    _raw_score = sum(w * v for w, v in zip(_weights, _values)) / _total_w

    # ── 거래량 보정 — 하위 20%일 때만 -0.05 ──────────────
    _vol_hist = [float(c[5]) for c in ohlcv_5m[-30:] if len(c) > 5]
    if len(_vol_hist) >= 20:
        _cur_vol = _vol_hist[-1]
        _vol_rank = sum(1 for v in _vol_hist if v <= _cur_vol)
        if _vol_rank / len(_vol_hist) <= 0.20:
            _raw_score = max(0.0, _raw_score - 0.05)

    # ── EMA 스무딩 (alpha=0.25, 이전 0.30보다 안정적) ────
    _alpha = 0.25
    if _regime_ema_pctl is None:
        _regime_ema_pctl = _raw_score
    else:
        _regime_ema_pctl = _alpha * _raw_score + (1 - _alpha) * _regime_ema_pctl
    _p = _regime_ema_pctl

    # ── 히스테리시스 + 레짐 결정 ──────────────────────────
    # ★ v10.13b: BAD 제거 → 3단 레짐 (LOW / NORMAL / HIGH)
    # 백테스트: BAD 제거 시 $760→$1751 (+130%), MDD -6.5%
    # BAD 구간 = 변동성 극대 = MR 최적 사냥터 → 차단이 오히려 손해
    if _regime_last == "LOW":
        new = "LOW" if _p < 0.60 else ("NORMAL" if _p < 0.70 else "HIGH")
    elif _regime_last == "NORMAL":
        new = "LOW" if _p < 0.50 else ("NORMAL" if _p < 0.73 else "HIGH")
    elif _regime_last == "HIGH":
        new = "LOW" if _p < 0.50 else ("NORMAL" if _p < 0.67 else "HIGH")
    else:
        new = "LOW" if _p < 0.55 else ("NORMAL" if _p < 0.70 else "HIGH")

    # ★ v10.15: HIGH sticky — HIGH 진입 후 5분 유지
    global _high_enter_ts
    from v9.config import HIGH_STICKY_SEC
    _now_ts = time.time()

    if new == "HIGH":
        _high_enter_ts = _now_ts  # 타이머 갱신 (재진입도 리셋)
    elif _high_enter_ts > 0 and (_now_ts - _high_enter_ts) < HIGH_STICKY_SEC:
        # HIGH에서 내려왔지만 5분 미경과 → HIGH 유지
        new = "HIGH"

    if new != _regime_last:
        print(f"[REGIME] {_regime_last} → {new} "
              f"(score={_p:.3f} | 5m={pctl_5m:.2f} 15m={pctl_15m:.2f} 1h={pctl_1h:.2f})")

    # ★ BAD 모드 히스테리시스
    global _bad_regime_active
    _bad_prev = _bad_regime_active
    if _p < BAD_ENTER_THRESH:
        _bad_regime_active = True
    elif _p >= BAD_EXIT_THRESH:
        _bad_regime_active = False
    if _bad_regime_active != _bad_prev:
        print(f"[BAD_REGIME] {'ON' if _bad_regime_active else 'OFF'} "
              f"(score={_p:.3f}, zombie_only)")

    _regime_last = new
    _regime_cache_snap_id = _snap_id
    _regime_cache_result = new
    return new


# ═════════════════════════════════════════════════════════════════
# BTC Crash Filter  (★ v10.6)
# ═════════════════════════════════════════════════════════════════
# ★ v10.12: 2중 크래시 감지
BTC_CRASH_1M_THRESHOLD = -0.004  # 1봉(1분) -0.4% — 순간 급락 (백테스트: 10분후 61% ★★)
BTC_CRASH_3M_THRESHOLD = -0.008  # 3봉(3분) -0.8% — 지속 하락 (검증완료 61%)
BTC_CRASH_FREEZE_SEC = 180       # 차단 3분

def _check_btc_crash(snapshot: "MarketSnapshot", system_state: dict) -> bool:
    """
    ★ v10.12: 2중 크래시 감지
      1) 1분봉 1봉 -0.4% → 순간 급락 (0.8/일, 오탐 적음)
      2) 1분봉 3봉 -0.8% → 지속 하락
    어느 쪽이든 발동 시 180초간 신규 OPEN + DCA 차단.
    """
    now = time.time()
    _freeze_until = float(system_state.get("btc_crash_freeze_until", 0.0) or 0.0)
    if now < _freeze_until:
        return True

    btc_pool = (snapshot.ohlcv_pool or {}).get("BTC/USDT", {})
    ohlcv_1m = btc_pool.get("1m", [])
    if len(ohlcv_1m) < 4:
        return False

    _p_now = float(ohlcv_1m[-1][4])

    # ── 조건1: 1분 -0.5% (플래시 크래시) ──
    _p_1ago = float(ohlcv_1m[-2][4])
    if _p_1ago > 0:
        _ret_1m = (_p_now - _p_1ago) / _p_1ago
        if _ret_1m <= BTC_CRASH_1M_THRESHOLD:
            system_state["btc_crash_freeze_until"] = now + BTC_CRASH_FREEZE_SEC
            print(f"[BTC_CRASH] ★ 1분 급락 {_ret_1m*100:.2f}% → {BTC_CRASH_FREEZE_SEC}초 차단")
            return True

    # ── 조건2: 3분 -0.8% (지속 하락) ──
    _p_3ago = float(ohlcv_1m[-4][4])
    if _p_3ago > 0:
        _ret_3m = (_p_now - _p_3ago) / _p_3ago
        if _ret_3m <= BTC_CRASH_3M_THRESHOLD:
            system_state["btc_crash_freeze_until"] = now + BTC_CRASH_FREEZE_SEC
            print(f"[BTC_CRASH] 3분 하락 {_ret_3m*100:.2f}% → {BTC_CRASH_FREEZE_SEC}초 차단")
            return True

    return False

def _pos_items(st: dict):
    """(symbol, p) 플랫 이터레이터 — hedge mode 양방향 지원.
    ★ v10.10 fix: iter_positions가 p_long/p_short 키에서 결정한 side를
    dict에 강제 주입. dict 내부 side 필드가 꼬여도 안전.
    """
    for sym, sym_st in st.items():
        if not isinstance(sym_st, dict):
            continue
        for pos_side, p in iter_positions(sym_st):
            if isinstance(p, dict):
                p["side"] = pos_side  # p_long→"buy", p_short→"sell" 강제
            yield sym, p
from v9.config import (
    LONG_ONLY_SYMBOLS, SHORT_ONLY_SYMBOLS,
    LEVERAGE, DCA_DISTANCES, DCA_WEIGHTS,
    TP1_PCT, TP1_PCT_BY_DCA, TP1_PARTIAL_RATIO,
    TP_ATR_POWER, TP_ATR_MIN_MULT, TP_ATR_MAX_MULT,
    TP2_PCT, TP2_PARTIAL_RATIO,
    TRAILING_TIMEOUT_MIN,
    DCA_COOLDOWN_BY_TIER, DCA_COOLDOWN_SEC,
    TOTAL_MAX_SLOTS, MAX_LONG, MAX_SHORT, GRID_DIVISOR, MAX_MR_PER_SIDE,
    HEDGE_OPEN_CORR_MIN, HEDGE_STAGE1_MULTIPLIER,
    HARD_SL_BASE, HARD_SL_FACTOR_MIN, HARD_SL_FACTOR_MAX, HARD_SL_ATR_BASE,
    TREND_FILTER_ENABLED, TREND_FILTER_DEADZONE, TREND_FILTER_MIN_BARS,
    DCA_MIN_CORR, ASYM_MAX_DCA_LEVEL,
    FALLING_KNIFE_BARS, FALLING_KNIFE_THRESHOLD,
    PULLBACK_DIST_ATR, ASYM_OPEN_RATIO, HEDGE_MODE,
    REBOUND_ALPHA,  # ★ v10.14c: min_roi 반등 TP1
    OPEN_CORR_MIN,  # ★ PATCH: config 통합
    TP_LOCK_SKEW_1, TP_LOCK_SKEW_2, TP_LOCK_RELEASE,
    TP_LOCK_STRESS_ROI, TP_LOCK_STRESS_MULT,
    TP_LOCK_MIN_ROI, TP_LOCK_EXIT_ROI,
    SKEW_STAGE2_TRIGGER, SKEW_HEAVY_TP_ROI_1, SKEW_HEAVY_TP_ROI_2,
)
from v9.utils.utils_math import (
    calc_rsi, calc_ema, atr_from_ohlcv, safe_float,
    calc_roi_pct, calc_roi_pct_net,
)


OPEN_ATR_TIGHTEN_MULT    = 1.5
OPEN_ATR_LOOSEN_MULT     = 0.7
OPEN_RSI_SHIFT           = 3
OPEN_SYMBOL_COOLDOWN_SEC = 10 * 60
OPEN_WAIT_NEXT_BAR       = False
OPEN_PENDING_TTL_SEC     = 5 * 60


ASYM_PENDING_TTL_SEC     = 3 * 60   # ASYM armed 최대 대기



def _tid() -> str:
    return str(uuid.uuid4())[:8]


# ═════════════════════════════════════════════════════════════════
# ★ V10.17: Slot Balance 규칙
# ═════════════════════════════════════════════════════════════════
_HEDGE_ROLES_SLOT = {"CORE_HEDGE", "INSURANCE_SH", "HEDGE", "SOFT_HEDGE"}

def _count_active_by_side(st: Dict) -> tuple:
    """활성 포지션 수 (롱, 숏) — HEDGE/INSURANCE 계열 제외 (calc_skew와 동일 기준)."""
    longs = shorts = 0
    for sym, sym_st in st.items():
        if not isinstance(sym_st, dict):
            continue
        for pos_side, p in iter_positions(sym_st):
            if isinstance(p, dict):
                if p.get("role", "") in _HEDGE_ROLES_SLOT:
                    continue
                if pos_side == "buy": longs += 1
                else: shorts += 1
    return longs, shorts


HEAVY_REBALANCE_SKEW    = 0.10   # skew ≥ 10%
HEAVY_REBALANCE_ROI_MIN = 2.0    # ROI ≥ +2%
HEAVY_REBALANCE_CD_SEC  = 300    # 5분 쿨다운
_heavy_rebal_cd = 0.0


def plan_heavy_rebalance(snapshot: MarketSnapshot, st: Dict,
                         exclude_syms: set = None) -> List[Intent]:
    """Rule C: Heavy side 수익 슬롯 1개 강제 익절 (skew ≥ 10%)."""
    global _heavy_rebal_cd
    intents: List[Intent] = []
    now = time.time()
    if now < _heavy_rebal_cd:
        return intents

    from v9.engines.hedge_core import calc_skew
    total_cap = float(getattr(snapshot, "real_balance_usdt", 0.0) or 0.0)
    skew_val, long_m, short_m = calc_skew(st, total_cap)
    if skew_val < HEAVY_REBALANCE_SKEW:
        return intents

    heavy_side = "buy" if long_m > short_m else "sell"
    prices = snapshot.all_prices or {}
    best = None
    best_roi = -999.0
    _excl = exclude_syms or set()

    for sym, sym_st in st.items():
        if not isinstance(sym_st, dict):
            continue
        if sym in _excl:
            continue
        p = get_p(sym_st, heavy_side)
        if not isinstance(p, dict):
            continue
        if p.get("step", 0) >= 1:
            continue
        if p.get("role", "") in ("HEDGE", "SOFT_HEDGE", "INSURANCE_SH", "CORE_HEDGE"):
            continue
        cp = float(prices.get(sym, 0) or 0)
        ep = float(p.get("ep", 0) or 0)
        if cp <= 0 or ep <= 0:
            continue
        roi = calc_roi_pct(ep, cp, heavy_side, LEVERAGE)
        if roi >= HEAVY_REBALANCE_ROI_MIN and roi > best_roi:
            best = (sym, p, roi, cp)
            best_roi = roi

    if best:
        sym, p, roi, cp = best
        close_side = "sell" if heavy_side == "buy" else "buy"
        intents.append(Intent(
            trace_id=_tid(),
            intent_type=IntentType.FORCE_CLOSE,
            symbol=sym, side=close_side,
            qty=float(p.get("amt", 0.0)), price=cp,
            reason=f"HEAVY_REBALANCE(skew={skew_val:.0%},roi={roi:+.1f}%)",
            metadata={"roi_pct": roi, "_expected_role": p.get("role", "")},
        ))
        _heavy_rebal_cd = now + HEAVY_REBALANCE_CD_SEC
        print(f"[HEAVY_REBALANCE] {sym} {heavy_side} roi={roi:+.1f}% skew={skew_val:.0%}")

    return intents


# ═════════════════════════════════════════════════════════════════
# 1h 추세 필터  (EMA20 vs EMA50)
# ═════════════════════════════════════════════════════════════════
def _trend_filter_side(symbol: str, snapshot: MarketSnapshot) -> set:
    """
    1h EMA20 vs EMA50 기반 진입 허용 방향 반환.

    Returns:
        {"buy", "sell"} — 양방향 허용 (데드존 or 데이터 부족)
        {"buy"}         — 하락추세: Long만 허용 (Short 차단)  ← 역방향 평균회귀
        {"sell"}        — 상승추세: Short만 허용 (Long 차단)
        set()           — 극단 추세: 양방향 차단 (사용 안 함 — 진입 기회 너무 줄어듦)

    방향 논리:
        상승추세(EMA20 > EMA50 + deadzone):
            → 추세 방향 = 상위, 평균회귀는 하락 방향에서 발생
            → Long(추세 추종) 차단, Short(평균회귀 대상) 허용
        하락추세(EMA20 < EMA50 - deadzone):
            → Long(평균회귀 대상) 허용, Short(추세 추종) 차단
    """
    # ★ v10.5: trend filter 비활성화 — MR 양방향 허용
    return {"buy", "sell"}
    if not TREND_FILTER_ENABLED:
        return {"buy", "sell"}

    pool   = (snapshot.ohlcv_pool or {}).get(symbol, {})
    ohlcv_1h = pool.get("1h", [])

    if len(ohlcv_1h) < TREND_FILTER_MIN_BARS:
        # 데이터 부족 → 필터 통과 (보수적 비차단)
        return {"buy", "sell"}

    closes_1h = [float(c[4]) for c in ohlcv_1h]
    ema20 = calc_ema(closes_1h, period=20)
    ema50 = calc_ema(closes_1h, period=50)

    if ema50 <= 0:
        return {"buy", "sell"}

    diff_pct = (ema20 - ema50) / ema50   # 양수 = 상승추세, 음수 = 하락추세

    if diff_pct > TREND_FILTER_DEADZONE:
        # 상승추세 → Long 신규 차단 (추세 방향 베팅 금지), Short 허용
        return {"sell"}
    elif diff_pct < -TREND_FILTER_DEADZONE:
        # 하락추세 → Short 신규 차단, Long 허용
        return {"buy"}
    else:
        # 데드존 내 → 양방향 허용
        return {"buy", "sell"}



# ═════════════════════════════════════════════════════════════════
# DCA 타겟 빌더
# ═════════════════════════════════════════════════════════════════
# DCA ROI 트리거 (avg_ep 기준 실시간 ROI)
# T2: -3.5% / T3: -5.0% / T4: -5.5%
DCA_ROI_TRIGGERS = {2: -8.25, 3: -8.25, 4: -8.25, 5: -8.25}

REGIME_HARD_SL       = {"BAD": -5.0, "LOW": -6.5, "NORMAL": -8.0, "HIGH": -10.0}
_REGIME_WIDTH = {"HIGH": 4, "NORMAL": 3, "LOW": 2, "BAD": 1}

def _wider_regime(a: str, b: str) -> str:
    return a if _REGIME_WIDTH.get(a, 0) >= _REGIME_WIDTH.get(b, 0) else b

def _build_dca_targets(
    entry_p: float, side: str, grid_notional: float,
    regime: str = "LOW",
) -> list:
    """V10.17: DCA 5단 타겟 — 전 레짐 동일 간격."""
    dca_w   = DCA_WEIGHTS
    total_w = sum(dca_w)
    targets = []
    for i, tier in enumerate([2, 3, 4, 5]):
        roi_trig = DCA_ROI_TRIGGERS.get(tier, -8.25)
        dist = abs(roi_trig) / 100 / LEVERAGE
        target_p = entry_p * (1.0 - dist) if side == "buy" else entry_p * (1.0 + dist)
        notional = grid_notional * (dca_w[i + 1] / total_w)
        targets.append({"tier": tier, "target_p": target_p,
                        "notional": notional, "roi_trigger": roi_trig})
    return targets




# ═════════════════════════════════════════════════════════════════
# ASYM_FORCE 플래너  (비대칭 슬롯 — 구조 복구 장치)
# ═════════════════════════════════════════════════════════════════
def _plan_open_asymmetric(
    snapshot: MarketSnapshot,
    st: Dict,
    system_state: Dict,
    long_targets: list,
    short_targets: list,
) -> List[Intent]:
    """
    슬롯 불균형 시 반대 방향 포지션을 단계적으로 확보.

    1) 슬롯 개방 조건 확인 (T2/T3/T4 기반)
    2) 부족한 방향 후보 심볼 ARMED (3분 대기)
    3) 3분 내 모멘텀 확인 후 OPEN intent 생성
    4) MR ≥ 0.9 이면 armed 취소 (신규 진입 금지)
    """
    from v9.config import (
        DYNAMIC_SLOT_EXPAND_3_TRIGGER,
        DYNAMIC_SLOT_EXPAND_4_TRIGGER,
        DYNAMIC_SLOT_EXPAND_4_ALT,
        KILLSWITCH_FREEZE_ALL_MR,
        ASYM_SIZE_RATIO,
        ASYM_MAX_DCA_LEVEL,
    )

    mr_now = float(getattr(snapshot, "margin_ratio", 0.0) or 0.0)

    # ── 현재 포지션 DCA 레벨 집계 ──────────────────────────────
    max_dca        = 0
    t2_plus_count  = 0
    t2_plus_long   = 0
    t2_plus_short  = 0
    t3_plus_long   = 0
    t3_plus_short  = 0
    t4_long        = 0
    t4_short       = 0

    for sym, sym_st in st.items():
        for pos_side, p in iter_positions(sym_st):
            dca  = int(p.get("dca_level", 1) or 1)
            side = pos_side
            if dca > max_dca:
                max_dca = dca
            if dca >= DYNAMIC_SLOT_EXPAND_3_TRIGGER:
                t2_plus_count += 1
                if side == "buy":   t2_plus_long  += 1
                else:               t2_plus_short += 1
            if dca >= 3:
                if side == "buy":   t3_plus_long  += 1
                else:               t3_plus_short += 1
            if dca >= 4:
                if side == "buy":   t4_long  += 1
                else:               t4_short += 1

    max_dca_long  = 0
    max_dca_short = 0
    for sym, sym_st in st.items():
        for pos_side, p2 in iter_positions(sym_st):
            d2 = int(p2.get("dca_level", 1) or 1)
            if pos_side == "buy"  and d2 > max_dca_long:  max_dca_long  = d2
            if pos_side == "sell" and d2 > max_dca_short: max_dca_short = d2

    # 숏 슬롯: 롱쪽 DCA가 깊을 때 개방 / 롱 슬롯: 숏쪽 DCA가 깊을 때 개방
    def _dyn_slots_for(src_max_dca: int, src_t2_count: int) -> int:
        if src_max_dca >= DYNAMIC_SLOT_EXPAND_4_TRIGGER or src_t2_count >= DYNAMIC_SLOT_EXPAND_4_ALT:
            return 4
        if src_max_dca >= DYNAMIC_SLOT_EXPAND_3_TRIGGER:
            return 3
        return 0  # 조건 미충족

    dyn_slots_long  = _dyn_slots_for(max_dca_short, t2_plus_short)  # 숏 깊음 → 롱 슬롯
    dyn_slots_short = _dyn_slots_for(max_dca_long,  t2_plus_long)   # 롱 깊음 → 숏 슬롯

    if dyn_slots_long == 0 and dyn_slots_short == 0:
        _cleanup_asym_pending(system_state, st)
        return []

    slots = count_slots(st)
    rl, rs = slots.risk_long, slots.risk_short

    # 방향별 목표 계산 (각 방향 독립)
    def _fill_target(dyn_slots: int, cur_cnt: int, t3_opp: int, t4_opp: int) -> int:
        if dyn_slots == 0:
            return 0
        if dyn_slots == 3:
            return 1 if cur_cnt == 0 else 0
        # dyn_slots == 4
        goal = 2
        if t3_opp >= 2 or t4_opp >= 1:
            goal = 3
        return max(0, goal - cur_cnt)

    fill_targets = {
        "buy":  _fill_target(dyn_slots_long,  rl, t3_plus_short, t4_short),
        "sell": _fill_target(dyn_slots_short, rs, t3_plus_long,  t4_long),
    }

    intents   : List[Intent] = []
    now_ts    = time.time()
    cd_map    = system_state.get("open_symbol_cd_until", {})
    total_cap = float(getattr(snapshot, "real_balance_usdt", 0.0) or 0.0)
    base_alloc    = total_cap / GRID_DIVISOR if GRID_DIVISOR > 0 else 0.0

    # ASYM 사이즈 = imbalance × ASYM_SIZE_RATIO (0.75)
    # imbalance: 손실 방향 노출 슬롯 수 - 반대 방향 노출 슬롯 수
    long_notional  = sum(
        float(p.get("amt", 0.0) or 0.0) * float(p.get("ep", 0.0) or 0.0)
        for sym_st in st.values()
        for side, p in iter_positions(sym_st)
        if side == "buy"
    )
    short_notional = sum(
        float(p.get("amt", 0.0) or 0.0) * float(p.get("ep", 0.0) or 0.0)
        for sym_st in st.values()
        for side, p in iter_positions(sym_st)
        if side == "sell"
    )
    imbalance_notional = abs(long_notional - short_notional)
    asym_notional = max(base_alloc * LEVERAGE, imbalance_notional * ASYM_SIZE_RATIO)
    grid_notional = asym_notional

    asym_pending = system_state.setdefault("pending_asym_force", {})

    # ── MR ≥ 0.9: 기존 armed 전부 취소, 신규 armed 금지 ─────────
    if mr_now >= KILLSWITCH_FREEZE_ALL_MR:
        for sym in list(asym_pending.keys()):
            print(f"[ASYM_FORCE] {sym} CANCELLED — MR≥0.9 freeze")
            asym_pending.pop(sym, None)
        return []

    # ── 신규 ARMED ───────────────────────────────────────────────
    for force_side, fill_count in [("buy", fill_targets["buy"]), ("sell", fill_targets["sell"])]:
        if fill_count <= 0:
            continue
        cands_pool = long_targets if force_side == "buy" else short_targets
        candidates = [
            s for s in cands_pool
            if not is_active(st.get(s, {}))
            and not get_pending_entry(st.get(s, {}), force_side)
            and float(cd_map.get(s, 0.0)) <= now_ts
            and s not in asym_pending
        ]
        my_count_now = rl if force_side == "buy" else rs
        for sym in candidates[:fill_count]:
            curr_p = float((snapshot.all_prices or {}).get(sym, 0.0))
            if curr_p <= 0:
                continue
            corr = (getattr(snapshot, "correlations", None) or {}).get(sym, 1.0)
            if corr < OPEN_CORR_MIN:
                continue
            asym_pending[sym] = {
                "side":             force_side,
                "armed_ts":         now_ts,
                "expire_ts":        now_ts + ASYM_PENDING_TTL_SEC,
                "dyn_slots":        dyn_slots_long if force_side == "buy" else dyn_slots_short,
                "reason_base":      f"ASYM_FORCE_{force_side.upper()}_DYN{dyn_slots_long if force_side == 'buy' else dyn_slots_short}",
                "grid_notional":    grid_notional,
                "t4_long":          t4_long,
                "t4_short":         t4_short,
                "my_count_at_arm":  my_count_now,
            }
            print(f"[ASYM_FORCE] {sym} ARMED side={force_side} expire={ASYM_PENDING_TTL_SEC}s")

    # ── ARMED 처리: 재검증 → 모멘텀 → OPEN intent 생성 ──────────
    to_remove = []
    for sym, pend in list(asym_pending.items()):
        if is_active(st.get(sym, {})) or get_pending_entry(st.get(sym, {})):
            to_remove.append(sym)
            continue

        force_side      = pend["side"]
        expire_ts       = float(pend["expire_ts"])
        armed_ts        = float(pend["armed_ts"])
        reason_base     = pend["reason_base"]
        pend_gn         = float(pend["grid_notional"])
        pend_t4_long    = int(pend.get("t4_long",  0))
        pend_t4_short   = int(pend.get("t4_short", 0))
        my_count_at_arm = int(pend.get("my_count_at_arm", 0))

        curr_p = float((snapshot.all_prices or {}).get(sym, 0.0))
        if curr_p <= 0:
            to_remove.append(sym)
            continue

        # 재검증: 내 방향 슬롯 이미 충족
        cur_slots = count_slots(st)
        my_count_now = cur_slots.risk_long if force_side == "buy" else cur_slots.risk_short
        if my_count_now > my_count_at_arm:
            to_remove.append(sym)
            print(f"[ASYM_FORCE] {sym} CANCELLED — 슬롯 충족 ({my_count_at_arm}→{my_count_now})")
            continue

        # 재검증: T4 압박 소멸
        cur_t4_long  = sum(
            1 for s2, s2st in st.items()
            for side, p in iter_positions(s2st)
            if side == "buy" and int(p.get("dca_level", 1) or 1) >= 4
        )
        cur_t4_short = sum(
            1 for s2, s2st in st.items()
            for side, p in iter_positions(s2st)
            if side == "sell" and int(p.get("dca_level", 1) or 1) >= 4
        )
        t4_side_gone = (
            (force_side == "buy"  and pend_t4_short > 0 and cur_t4_short == 0) or
            (force_side == "sell" and pend_t4_long  > 0 and cur_t4_long  == 0)
        )
        if t4_side_gone:
            to_remove.append(sym)
            print(f"[ASYM_FORCE] {sym} CANCELLED — T4 압박 소멸")
            continue

        # ── 모멘텀 확인 ────────────────────────────────────────
        fire        = False
        fire_reason = ""
        elapsed     = now_ts - armed_ts

        pool     = (snapshot.ohlcv_pool or {}).get(sym, {})
        ohlcv_5m = pool.get("5m", [])

        if len(ohlcv_5m) >= 3:
            closes_5m = [float(x[4]) for x in ohlcv_5m]
            rsi5_now  = calc_rsi(closes_5m,      period=14) if len(closes_5m) >= 15 else 50.0
            rsi5_prev = calc_rsi(closes_5m[:-1], period=14) if len(closes_5m) >= 16 else 50.0

            rsi_turn = (
                (force_side == "buy"  and rsi5_now > rsi5_prev) or
                (force_side == "sell" and rsi5_now < rsi5_prev)
            )
            c2, c1, c0 = closes_5m[-1], closes_5m[-2], closes_5m[-3]
            consec = (
                (force_side == "buy"  and c2 > c1 > c0) or
                (force_side == "sell" and c2 < c1 < c0)
            )
            ema9   = calc_ema(closes_5m, period=9)
            ema_ok = (
                (force_side == "buy"  and curr_p > ema9) or
                (force_side == "sell" and curr_p < ema9)
            )

            if rsi_turn:
                fire = True; fire_reason = "ASYM_MOMENTUM_RSI"
            elif consec:
                fire = True; fire_reason = "ASYM_MOMENTUM_CONSEC"
            elif ema_ok:
                fire = True; fire_reason = "ASYM_MOMENTUM_EMA9"

        # 3분 만료 → fallback 강제진입 (MR 0.9 미만일 때만 — 위에서 이미 guard)
        if not fire and elapsed >= ASYM_PENDING_TTL_SEC:
            fire = True; fire_reason = "ASYM_FALLBACK_3MIN"

        if not fire:
            continue

        # ── OPEN intent 생성 ──────────────────────────────────
        t1_notional = pend_gn * (DCA_WEIGHTS[0] / sum(DCA_WEIGHTS))
        qty = t1_notional / curr_p
        if qty <= 0:
            to_remove.append(sym)
            continue

        system_state.setdefault("open_symbol_cd_until", {})[sym] = now_ts + OPEN_SYMBOL_COOLDOWN_SEC
        to_remove.append(sym)
        print(f"[ASYM_FORCE] {sym} FIRE reason={fire_reason} elapsed={elapsed:.0f}s")

        intents.append(Intent(
            trace_id=_tid(),
            intent_type=IntentType.OPEN,
            symbol=sym,
            side=force_side,
            qty=qty,
            price=curr_p,
            reason=f"{reason_base}_{fire_reason}",
            metadata={
                "atr":         0.0,
                "dca_targets": _build_dca_targets(curr_p, force_side, pend_gn),
                "role":        "BALANCE",
            },
        ))

    for sym in to_remove:
        asym_pending.pop(sym, None)

    # ── T4 도달 시 반대방향 수익 포지션 DCA 보강 ─────────────────
    for t4_side, t4_cnt, opp_side in [
        ("buy",  t4_long,  "sell"),
        ("sell", t4_short, "buy"),
    ]:
        if t4_cnt <= 0:
            continue

        total_cap2  = float(getattr(snapshot, "real_balance_usdt", 0.0) or 0.0)
        grid_not2   = (total_cap2 / GRID_DIVISOR) * LEVERAGE if GRID_DIVISOR > 0 else 0.0
        dca_w2      = DCA_WEIGHTS
        total_w2    = sum(dca_w2)

        # T2 DCA: 반대방향 T1 포지션 중 ROI 절댓값 최소
        opp_t1 = []
        for sym, sym_st in st.items():
            p = get_p(sym_st, opp_side)
            if not isinstance(p, dict):
                continue
            if p.get("pending_dca"):
                continue
            if int(p.get("dca_level", 1) or 1) >= 2:
                continue
            curr_px = float((snapshot.all_prices or {}).get(sym, 0.0) or 0.0)
            if curr_px <= 0:
                continue
            roi = calc_roi_pct(p.get("ep", 0.0), curr_px, opp_side, LEVERAGE)
            opp_t1.append((sym, p, curr_px, roi))

        if opp_t1:
            best_sym, best_p, best_px, _ = min(opp_t1, key=lambda x: abs(x[3]))
            t2_notional = grid_not2 * (dca_w2[1] / total_w2)
            add_qty = t2_notional / best_px
            if add_qty > 0:
                best_p["pending_dca"] = {"tier": 2, "ts": time.time()}
                intents.append(Intent(
                    trace_id=_tid(),
                    intent_type=IntentType.DCA,
                    symbol=best_sym,
                    side=opp_side,
                    qty=add_qty,
                    price=best_px,
                    reason=f"T4_OPP_DCA_T2_{opp_side.upper()}",
                    metadata={"tier": 2, "role": "BALANCE"},
                ))

        # T3 DCA: T4 2개 이상 + 반대방향 T3 최대 1개 제한
        if t4_cnt >= 2:
            t3_exist = sum(
                1 for s2, s2st in st.items()
                for side, p2 in iter_positions(s2st)
                if side == opp_side and int(p2.get("dca_level", 1) or 1) >= 3
            )
            if t3_exist < 1:
                opp_t2 = []
                for sym, sym_st in st.items():
                    p = get_p(sym_st, opp_side)
                    if not isinstance(p, dict):
                        continue
                    if p.get("pending_dca"):
                        continue
                    if int(p.get("dca_level", 1) or 1) != 2:
                        continue
                    curr_px = float((snapshot.all_prices or {}).get(sym, 0.0) or 0.0)
                    if curr_px <= 0:
                        continue
                    roi = calc_roi_pct(p.get("ep", 0.0), curr_px, opp_side, LEVERAGE)
                    opp_t2.append((sym, p, curr_px, roi))

                if opp_t2:
                    t3_sym, t3_p, t3_px, _ = min(opp_t2, key=lambda x: abs(x[3]))
                    t3_notional = grid_not2 * (dca_w2[2] / total_w2)
                    t3_qty = t3_notional / t3_px
                    if t3_qty > 0:
                        t3_p["pending_dca"] = {"tier": 3, "ts": time.time()}
                        intents.append(Intent(
                            trace_id=_tid(),
                            intent_type=IntentType.DCA,
                            symbol=t3_sym,
                            side=opp_side,
                            qty=t3_qty,
                            price=t3_px,
                            reason=f"T4x2_OPP_DCA_T3_{opp_side.upper()}",
                            metadata={"tier": 3, "role": "BALANCE"},
                        ))

    return intents


def _cleanup_asym_pending(system_state: dict, st: dict) -> None:
    """asym_pending에서 이미 포지션이 생긴 심볼 정리."""
    asym_pending = system_state.get("pending_asym_force", {})
    to_rm = [s for s in asym_pending if is_active(st.get(s, {}))]
    for s in to_rm:
        asym_pending.pop(s, None)

# ═════════════════════════════════════════════════════════════════
# ★ v10.10: _plan_asym_mr_fail (HH) 제거
# CORE_HEDGE + INSURANCE_SH + HARD_SL -3% 로 대체
# 기존 HEDGE role 포지션은 plan_force_close/plan_trail_on에서 레거시 호환
# ═════════════════════════════════════════════════════════════════


# ═════════════════════════════════════════════════════════════════
# Falling Knife Filter  (★ v9.9)
# ═════════════════════════════════════════════════════════════════
def _is_falling_knife_long(ohlcv_5m: list) -> bool:
    """최근 N개 5m 봉 누적 하락 > threshold → Long 차단"""
    if len(ohlcv_5m) < FALLING_KNIFE_BARS:
        return False
    closes = [float(x[4]) for x in ohlcv_5m[-FALLING_KNIFE_BARS:]]
    if closes[0] <= 0:
        return False
    return (closes[-1] - closes[0]) / closes[0] < -FALLING_KNIFE_THRESHOLD


def _is_falling_knife_short(ohlcv_5m: list) -> bool:
    """최근 N개 5m 봉 누적 상승 > threshold → Short 차단"""
    if len(ohlcv_5m) < FALLING_KNIFE_BARS:
        return False
    closes = [float(x[4]) for x in ohlcv_5m[-FALLING_KNIFE_BARS:]]
    if closes[0] <= 0:
        return False
    return (closes[-1] - closes[0]) / closes[0] > FALLING_KNIFE_THRESHOLD


# ═════════════════════════════════════════════════════════════════

# ═════════════════════════════════════════════════════════════════
# ASYM 슬롯 킬 대상 탐색  (★ v9.9)
# ═════════════════════════════════════════════════════════════════
def _find_kill_target(st: dict, target_side: str, snapshot: MarketSnapshot) -> Optional[str]:
    """
    target_side 슬롯 확보를 위해 킬할 포지션 탐색.
    1순위: max_roi_seen=0 포지션 중 ROI 최저
    2순위: 전체 중 ROI 최저
    ASYM 포지션은 킬 대상 제외.
    """
    from v9.utils.utils_math import calc_roi_pct
    mr0_cands: list = []
    all_cands: list = []

    for sym, sym_st in st.items():
        p = get_p(sym_st, target_side)
        if not isinstance(p, dict):
            continue
        curr_p = float((snapshot.all_prices or {}).get(sym, 0.0) or 0.0)
        if curr_p <= 0:
            continue

        roi = calc_roi_pct(p.get("ep", 0.0), curr_p, target_side, LEVERAGE)
        max_roi = float(p.get("max_roi_seen", 0.0) or 0.0)

        all_cands.append((sym, roi))
        if max_roi <= 0:
            mr0_cands.append((sym, roi))

    if mr0_cands:
        return min(mr0_cands, key=lambda x: x[1])[0]
    if all_cands:
        return min(all_cands, key=lambda x: x[1])[0]
    return None


# ═════════════════════════════════════════════════════════════════
# OPEN Planner  (일반 OPEN — 평균회귀 하이브리드 알파)
# ═════════════════════════════════════════════════════════════════
def plan_open(
    snapshot: MarketSnapshot,
    st: Dict,
    cooldowns: Dict,
    system_state: Dict,
) -> List[Intent]:
    intents: List[Intent] = []
    long_targets  = list(getattr(snapshot, "global_targets_long",  None) or [])
    short_targets = list(getattr(snapshot, "global_targets_short", None) or [])
    # ★ Python UnboundLocalError 방지: 루프 안에서 재할당되는 변수 미리 초기화
    total_cap = float(getattr(snapshot, "real_balance_usdt", 0.0) or 0.0)

    # ── stale pending_nextbar 정리 (재시작 후 TTL 초과 엔트리) ────
    pending_map = system_state.setdefault("open_pending_nextbar", {})
    now_ts = time.time()
    stale = [
        sym for sym, pend in list(pending_map.items())
        if isinstance(pend, dict)
        and (now_ts - int(pend.get("armed_ts_ms", 0)) / 1000.0) > OPEN_PENDING_TTL_SEC
    ]
    for sym in stale:
        pending_map.pop(sym, None)
    if stale:
        print(f"[plan_open] stale pending_nextbar 정리: {stale}")

    # ── 비대칭 강제 진입 ──────────────────────────────────────
    _asym_intents = []  # 레거시

    # ★ v10.10: HH 제거 — _plan_asym_mr_fail 호출 삭제

    _asym_syms = {i.symbol for i in _asym_intents}

    # ★ v10.9: 레짐 판정을 먼저 (크래시보다 선행)
    _btc_regime = _btc_vol_regime(snapshot)
    # ★ v10.13b: BAD 제거됨 — 이 조건은 더 이상 발동하지 않음

    # ★ v10.9: BTC Crash Filter — 급락 시 롱만 차단 (숏 MR은 허용)
    _btc_crash_active = _check_btc_crash(snapshot, system_state)

    _long_atr_base = 2.4
    _short_atr_base = 1.8

    # ── SOURCE_LINKED_HEDGE_CORE (v10.11: hedge_core 모듈) ───
    _skew, _long_margin, _short_margin = calc_skew(st, total_cap)

    # CORE 카운트 (FORCE_BALANCE reason용)
    _core_long = 0; _core_short = 0
    for _fb_s, _fb_ss in st.items():
        for _fb_side, _fb_pp in iter_positions(_fb_ss):
            if not isinstance(_fb_pp, dict): continue
            if _fb_pp.get("role") in ("HEDGE","SOFT_HEDGE","INSURANCE_SH","CORE_HEDGE"): continue
            if _fb_pp.get("step",0)>=1: continue
            if _fb_side=="buy": _core_long+=1
            else: _core_short+=1

    from v9.config import SKEW_HEDGE_TRIGGER
    _hc_intents = plan_hedge_core_entry(
        snapshot, st, _skew, _long_margin, _short_margin,
        total_cap, _btc_regime, _asym_syms, skew_thresh=SKEW_HEDGE_TRIGGER,
    )
    intents += _hc_intents
    _slhc_count = len(_hc_intents)

    # ════════════════════════════════════════════════════════════
    # [2순위] FORCE_BALANCE — ★ v10.12: 비활성화
    # 11건 $+1.38 (10승 +$12, 1패 -$11) — 실질 제로
    # CORE_HEDGE만으로 스큐 관리 충분, 이상한 타이밍 진입 방지
    # ════════════════════════════════════════════════════════════

    # ── 일반 OPEN 루프 ────────────────────────────────────────────
    # ★ v10.15: MR 슬롯은 CORE_MR만 카운트 (HEDGE는 별도 관리)
    # 전체 하드캡 5는 모든 포지션 카운트
    _slots_all = count_slots(st)
    if _slots_all.risk_total >= TOTAL_MAX_SLOTS:
        return intents  # 전체 5개 꽉 참
    _slots_mr = count_slots(st, role_filter="CORE_MR")
    if _slots_mr.risk_long >= MAX_MR_PER_SIDE:
        long_targets = []
    if _slots_mr.risk_short >= MAX_MR_PER_SIDE:
        short_targets = []
    if not long_targets and not short_targets:
        return intents

    # ★ v10.14: ATR 불균형 부스트 사전 계산 (루프 밖 1회, 변경시만 로그)
    _imbalance_ls = _core_short - _core_long
    _atr_boost_long  = min(0, -0.2 * max(0, _imbalance_ls) / 3)
    _atr_boost_short = min(0, -0.2 * max(0, -_imbalance_ls) / 3)
    _mr_atr_mult_long  = _long_atr_base + _atr_boost_long
    _mr_atr_mult_short = _short_atr_base + _atr_boost_short
    _dyn_atr_key = (round(_mr_atr_mult_long, 2), round(_mr_atr_mult_short, 2))
    if (_atr_boost_long != 0 or _atr_boost_short != 0):
        _prev = getattr(plan_open, '_last_dyn_atr', None)
        if _prev != _dyn_atr_key:
            print(f"[DYN_ATR] coreMR L={_core_long} S={_core_short} → "
                  f"atrL={_mr_atr_mult_long:.1f} atrS={_mr_atr_mult_short:.1f}")
            plan_open._last_dyn_atr = _dyn_atr_key

    # ★ V10.18: Slot Balance 루프 밖 1회 캐싱
    _open_longs, _open_shorts = _count_active_by_side(st)

    for symbol in list(set(long_targets + short_targets)):
        # [UnboundLocalError 방지] Python 스코프 선점 — 반드시 루프 최상단
        can_long  = False
        can_short = False
        _pend_entry_type = None  # ★ v10.14: 스코프 오염 방지 (dir() 제거)

        if symbol in _asym_syms:
            continue
        sym_st = st.get(symbol, {})
        if is_active(sym_st) or get_pending_entry(sym_st):
            continue

        # ★ v10.9: 같은 심볼 반대방향에 CORE 포지션 있으면 MR 진입 차단
        # (HEDGE/BALANCE만 반대방향 허용, CORE_MR vs CORE_MR 동시 보유 방지)
        _opp_buy  = get_p(sym_st, "buy")
        _opp_sell = get_p(sym_st, "sell")
        _has_core_long  = isinstance(_opp_buy, dict) and _opp_buy.get("role", "").startswith("CORE")
        _has_core_short = isinstance(_opp_sell, dict) and _opp_sell.get("role", "").startswith("CORE")

        if float(sym_st.get("exit_fail_cooldown_until", 0.0) or 0.0) > time.time():
            continue

        # ★ v10.15: MR 슬롯은 CORE_MR만, 전체 하드캡 5
        _slots_pre = count_slots(st)
        if _slots_pre.risk_total >= TOTAL_MAX_SLOTS:
            break  # 전체 5개 꽉 참
        _slots_mr_pre = count_slots(st, role_filter="CORE_MR")
        _can_long  = symbol in long_targets  and _slots_mr_pre.risk_long  < MAX_MR_PER_SIDE
        _can_short = symbol in short_targets and _slots_mr_pre.risk_short < MAX_MR_PER_SIDE
        if not (_can_long or _can_short):
            continue

        curr_p = float((snapshot.all_prices or {}).get(symbol, 0.0))
        if curr_p <= 0:
            continue

        pool      = (snapshot.ohlcv_pool or {}).get(symbol, {})
        ohlcv_1m  = pool.get("1m", [])
        ohlcv_5m  = pool.get("5m", [])
        ohlcv_15m = pool.get("15m", [])
        if len(ohlcv_1m) < 15 or len(ohlcv_15m) < 15 or len(ohlcv_5m) < 21:
            continue

        # ── (1) RSI 파라미터 (v10.2: 35/65 — ATR 보정 유지)
        rsi5_os = 35
        rsi5_ob = 65

        # ── (2) 코인 ATR% 보정
        atr_coin     = atr_from_ohlcv(ohlcv_1m[-15:], period=10)
        atr_pct_coin = atr_coin / curr_p if curr_p > 0 else HARD_SL_ATR_BASE
        atr_mult     = atr_pct_coin / HARD_SL_ATR_BASE if HARD_SL_ATR_BASE > 0 else 1.0

        shift = 0
        if   atr_mult > OPEN_ATR_TIGHTEN_MULT: shift =  OPEN_RSI_SHIFT
        elif atr_mult < OPEN_ATR_LOOSEN_MULT:  shift = -1

        adj_rsi5_os = max(20, rsi5_os - shift)
        adj_rsi5_ob = min(80, rsi5_ob + shift)

        # ── (2-b) BTC 변동성 레짐 보정 — ATR 배수 레짐별 조정 (루프 밖에서 계산됨)

        # ── (3) can_long/can_short 확정 + 추세 필터 먼저 적용
        # [BUG-1,2,3 FIX] mr_long_ok 계산 전에:
        #   - _can_long 사용 (슬롯 체크 포함)
        #   - trend filter 먼저 적용
        can_long  = _can_long
        can_short = _can_short

        # ★ v10.5: 심볼 방향 바이어스 필터
        if symbol in LONG_ONLY_SYMBOLS:  can_short = False
        if symbol in SHORT_ONLY_SYMBOLS: can_long  = False

        # ★ v10.9: 같은 심볼 반대방향 CORE 있으면 MR 차단 (CORE vs CORE 동시 보유 방지)
        if _has_core_short: can_long  = False  # 숏 CORE 있으면 롱 MR 차단
        if _has_core_long:  can_short = False  # 롱 CORE 있으면 숏 MR 차단

        allowed_sides = _trend_filter_side(symbol, snapshot)
        if can_long  and "buy"  not in allowed_sides: can_long  = False
        if can_short and "sell" not in allowed_sides: can_short = False

        # ★ v10.9: BTC Crash 중에는 롱만 차단 (숏 MR은 급락 반등 반대쪽이라 허용)
        if _btc_crash_active:
            can_long = False

        if not (can_long or can_short):
            continue

        # ── (4) 15m EMA — 롱 EMA10, 숏 EMA5  ★ v10.12
        closes_15m  = [float(x[4]) for x in ohlcv_15m]
        ema_period  = 20 if len(closes_15m) >= 20 else max(5, len(closes_15m))
        ema_20_15m  = calc_ema(closes_15m, period=ema_period)
        # ★ v10.12: 롱 전용 EMA10
        ema_10_15m  = calc_ema(closes_15m, period=10) if len(closes_15m) >= 10 else ema_20_15m
        # ★ v10.12: 숏 전용 EMA5 (빠른 추적)
        ema_5_15m   = calc_ema(closes_15m, period=5) if len(closes_15m) >= 5 else ema_10_15m

        closes_5m_all = [float(x[4]) for x in ohlcv_5m]
        ema_20_5m     = calc_ema(closes_5m_all, period=20) if len(closes_5m_all) >= 20 else 0.0

        # MR ATR 배수: 추세 강도에 따라 스무스 조절
        # EMA 괴리율로 추세 강도 측정
        _ema_gap = abs(ema_20_5m - ema_20_15m) / ema_20_15m if ema_20_15m > 0 else 0.0
        # ★ v10.14: ATR 부스트는 루프 밖에서 사전 계산됨 → 바로 사용

        # ★ v10.7: Skew 완화는 plan_soft_hedge에서 미러링으로 처리 (진입조건 왜곡 제거)

        # ★ v10.12: 롱 EMA10, 숏 EMA5
        mr_long_ok  = can_long  and ema_10_15m > 0 and (curr_p < ema_10_15m - atr_coin * _mr_atr_mult_long)
        # ★ PATCH: 숏 OR 조건 — 기존(EMA5+1.8×) OR 롱 미러링(EMA10+2.4×)
        # 횡보장에서 EMA5가 너무 빠르게 따라와 기존 조건이 거의 안 충족됨
        # EMA10 기준 미러링은 롱과 동일한 거리 감지 → 대칭적 MR 포착
        _mr_short_ema5  = ema_5_15m  > 0 and curr_p > ema_5_15m  + atr_coin * _mr_atr_mult_short
        _mr_short_ema10 = ema_10_15m > 0 and curr_p > ema_10_15m + atr_coin * _mr_atr_mult_long
        mr_short_ok = can_short and (_mr_short_ema5 or _mr_short_ema10)

        # ★ v10.7: TDECAY 제거 — ZOMBIE로 통합

        # ── (4-b) RSI 트리거 (5m RSI14 기준)
        closes_5m_rsi = [float(x[4]) for x in ohlcv_5m]
        rsi5_now  = calc_rsi(closes_5m_rsi, period=14) if len(closes_5m_rsi) >= 15 else 50.0
        rsi5_prev = calc_rsi(closes_5m_rsi[:-1], period=14) if len(closes_5m_rsi) >= 16 else 50.0
        long_trig  = (rsi5_now <= adj_rsi5_os) or (rsi5_now > rsi5_prev and rsi5_prev <= adj_rsi5_os)
        short_trig = (rsi5_now >= adj_rsi5_ob) or (rsi5_now < rsi5_prev and rsi5_prev >= adj_rsi5_ob)

        # ── 1분봉 마이크로 모멘텀 (3봉 중 2봉 이상)
        closes_1m_raw = [float(x[4]) for x in ohlcv_1m]
        opens_1m_raw  = [float(x[1]) for x in ohlcv_1m]
        _bull_cnt = sum(1 for c, o in zip(closes_1m_raw[-3:], opens_1m_raw[-3:]) if c > o)
        _bear_cnt = sum(1 for c, o in zip(closes_1m_raw[-3:], opens_1m_raw[-3:]) if c < o)
        micro_long_ok  = _bull_cnt >= 2
        micro_short_ok = _bear_cnt >= 2

        # ★ v10.7: Breakout — HIGH 레짐 한정, 보수적 추세추종
        # 조건: (1) BTC HIGH 레짐 (2) 5m 채널 돌파 (3) 거래량 확장 (4) EMA 정배열 (5) 마이크로 모멘텀
        # EMA 방향: 롱 = 5m > 15m (단기 모멘텀 리드), 숏 = 5m < 15m
        _highs_5m_20 = [float(x[2]) for x in ohlcv_5m[-21:-1]] if len(ohlcv_5m) >= 21 else []
        _lows_5m_20  = [float(x[3]) for x in ohlcv_5m[-21:-1]] if len(ohlcv_5m) >= 21 else []
        _highest_5m  = max(_highs_5m_20) if _highs_5m_20 else 0.0
        _lowest_5m   = min(_lows_5m_20)  if _lows_5m_20  else 0.0
        _vols_5m     = [float(x[5]) for x in ohlcv_5m[-21:] if len(x) > 5]
        _vol_now     = _vols_5m[-1] if _vols_5m else 0.0
        _vol_ma20    = (sum(_vols_5m[:-1]) / len(_vols_5m[:-1])) if len(_vols_5m) > 1 else 0.0
        _vol_ratio   = (_vol_now / _vol_ma20) if _vol_ma20 > 0 else 0.0

        # ★ v10.8: Breakout — 스큐 tier1+(12%p) 위기 시에만 발동
        #   평상시 진입은 MR 전담, Breakout은 마진 쏠림 위기 보정용
        _bo_vol_thresh = 1.5 if _btc_regime == "HIGH" else 2.0
        _bo_gate = (_skew >= 0.12
                    and _btc_regime in ("HIGH", "NORMAL")
                    and _vol_ratio >= _bo_vol_thresh)

        # ★ v10.12: Breakout 제거 — MR 전략만 사용
        # 3/16~17 BREAKOUT 진입이 HARD_SL 연타로 -$20+ 손실 발생
        # MR이 충분히 커버하므로 Breakout 불필요
        _bo_long_final  = False
        _bo_short_final = False

        # 최종 트리거 — MR 우선, Breakout은 MR 미발동 시에만
        _mr_long_final  = mr_long_ok  and long_trig  and micro_long_ok
        _mr_short_final = mr_short_ok and short_trig and micro_short_ok
        _bo_long_final  = _bo_long_final  and not _mr_long_final
        _bo_short_final = _bo_short_final and not _mr_short_final

        # ★ V10.16: E30(5m) OR — 장기 EMA 기준 추가 진입
        # 백테스트 R4_E30_loose_sym: Net+3405, MDD-25.7%, L$/S$ 양쪽 수익
        # 기존 E10(15m) 조건 미발동 시에만 OR로 추가
        _e30_long_final = False
        _e30_short_final = False
        closes_5m_e30 = [float(x[4]) for x in ohlcv_5m]
        if len(closes_5m_e30) >= 30:
            _ema30_5m = calc_ema(closes_5m_e30, period=30)
            # ── E30 Long 주 조건 (ATR2.0, RSI<40) ──
            # ★ v10.20: MR 우선순위 게이트 제거 → MR과 독립 평가 (로그 비교용)
            if can_long and _ema30_5m > 0:
                if curr_p < _ema30_5m - atr_coin * 2.0 and rsi5_now < 40 and micro_long_ok:
                    _e30_long_final = True
            # ── E30 Long 대칭 (숏 조건 뒤집기: ATR1.4, RSI<40) ──
            if can_long and not _e30_long_final and _ema30_5m > 0:
                if curr_p < _ema30_5m - atr_coin * 1.4 and rsi5_now < 40 and micro_long_ok:
                    _e30_long_final = True
            # ── E30 Short 주 조건 (ATR1.4, RSI>60) ──
            if can_short and _ema30_5m > 0:
                if curr_p > _ema30_5m + atr_coin * 1.4 and rsi5_now > 60 and micro_short_ok:
                    _e30_short_final = True
            # ── E30 Short 대칭 (롱 조건 뒤집기: ATR2.0, RSI>60) ──
            if can_short and not _e30_short_final and _ema30_5m > 0:
                if curr_p > _ema30_5m + atr_coin * 2.0 and rsi5_now > 60 and micro_short_ok:
                    _e30_short_final = True

        final_long_trig  = _mr_long_final  or _bo_long_final  or _e30_long_final
        final_short_trig = _mr_short_final or _bo_short_final or _e30_short_final


        # (reason 태깅은 pending_map/entry_type_tag에서 처리)

        # ★ v9.9: Falling Knife Filter — 급가속 구간 진입 차단
        if final_long_trig  and _is_falling_knife_long(ohlcv_5m):
            final_long_trig  = False
        if final_short_trig and _is_falling_knife_short(ohlcv_5m):
            final_short_trig = False

        # ── (5) pending-nextbar
        cd_map      = system_state.setdefault("open_symbol_cd_until", {})
        if float(cd_map.get(symbol, 0.0)) > now_ts:
            continue

        trigger_side = None
        reason       = ""
        last_5m_ts   = int(ohlcv_5m[-1][0])

        pend = pending_map.get(symbol)
        if OPEN_WAIT_NEXT_BAR and isinstance(pend, dict) and pend.get("armed"):
            armed_ts_ms   = int(pend.get("armed_ts_ms") or 0)
            armed_side    = pend.get("side")
            armed_age_sec = (last_5m_ts - armed_ts_ms) / 1000.0

            if armed_age_sec > OPEN_PENDING_TTL_SEC:
                pending_map.pop(symbol, None)
            elif armed_side in ("buy", "sell") and last_5m_ts > armed_ts_ms:
                trigger_side      = armed_side
                reason            = pend.get("reason", "HF_5M_NEXTBAR")
                _pend_entry_type  = pend.get("entry_type", None)  # 발화 시 armed 시점 entry_type 사용
                pending_map.pop(symbol, None)

        if trigger_side is None:
            if final_short_trig:
                if OPEN_WAIT_NEXT_BAR:
                    entry_type_tag = "MR" if _mr_short_final else ("E30" if _e30_short_final else "BREAKOUT")
                    _atr_label = f"ATR({atr_mult:.1f}x)" if _mr_short_final else f"E30_ATR({atr_mult:.1f}x)"
                    pending_map[symbol] = {
                        "armed":       True,
                        "armed_ts_ms": last_5m_ts,
                        "side":        "sell",
                        "entry_type":  entry_type_tag,
                        "reason": (
                            f"HF_{entry_type_tag}_5mRSI({rsi5_now:.0f}/{adj_rsi5_ob})_{_atr_label}"
                        ),
                    }
                    continue
                else:
                    trigger_side = "sell"
                    _et = "MR" if _mr_short_final else ("E30" if _e30_short_final else "BREAKOUT")
                    reason = f"HF_{_et}_5mRSI_ATR({atr_mult:.1f}x)"

            if final_long_trig and trigger_side is None:
                if OPEN_WAIT_NEXT_BAR:
                    entry_type_tag = "MR" if _mr_long_final else ("E30" if _e30_long_final else "BREAKOUT")
                    _atr_label = f"ATR({atr_mult:.1f}x)" if _mr_long_final else f"E30_ATR({atr_mult:.1f}x)"
                    pending_map[symbol] = {
                        "armed":       True,
                        "armed_ts_ms": last_5m_ts,
                        "side":        "buy",
                        "entry_type":  entry_type_tag,
                        "reason": (
                            f"HF_{entry_type_tag}_5mRSI({rsi5_now:.0f}/{adj_rsi5_os})_{_atr_label}"
                        ),
                    }
                    continue
                else:
                    trigger_side = "buy"
                    _et = "MR" if _mr_long_final else "BREAKOUT"
                    reason = f"HF_{_et}_5mRSI_ATR({atr_mult:.1f}x)"

        if trigger_side is None:
            continue

        # ★ V10.17 Rule A: Slot Balance Gate — 반대=0 AND 이쪽≥3 → 차단
        if trigger_side == "buy":
            if _open_shorts == 0 and _open_longs >= 3:
                continue
        else:
            if _open_longs == 0 and _open_shorts >= 3:
                continue

        if float(sym_st.get("open_fail_cooldown_until",   0.0)) > time.time(): continue
        if float(sym_st.get("reduce_fail_cooldown_until", 0.0)) > time.time(): continue

        # corr 최종 게이트
        corr = (getattr(snapshot, "correlations", None) or {}).get(symbol, 1.0)
        if corr < OPEN_CORR_MIN:
            continue

        # ── entry_type 확정 ─────────────────────────────────────
        if _pend_entry_type is not None:
            entry_type_tag = _pend_entry_type   # pending 발화: armed 시점 값 사용
        else:
            _is_mr  = (trigger_side == "buy"  and _mr_long_final)  or \
                      (trigger_side == "sell" and _mr_short_final)
            _is_e30 = (trigger_side == "buy"  and _e30_long_final) or \
                      (trigger_side == "sell" and _e30_short_final)
            _is_bo  = (trigger_side == "buy"  and _bo_long_final)  or \
                      (trigger_side == "sell" and _bo_short_final)
            # ★ v10.20: MR/E30 동시 발동 → "MR_E30", 단독 → 각자 태깅
            if _is_mr and _is_e30:
                entry_type_tag = "MR_E30"
            elif _is_mr:
                entry_type_tag = "MR"
            elif _is_e30:
                entry_type_tag = "E30"
            elif _is_bo:
                entry_type_tag = "BREAKOUT"
            else:
                entry_type_tag = "MR"
        _pend_entry_type = None  # 다음 심볼 오염 방지

        # ── 수량 계산 ───────────────────────────────────────────
        total_cap     = float(getattr(snapshot, "real_balance_usdt", 0.0) or 0.0)
        grid_notional = (total_cap / GRID_DIVISOR) * LEVERAGE

        # MR: 동일 비중, 물타기 DCA
        dca_w       = DCA_WEIGHTS
        total_w     = sum(dca_w)
        t1_notional = grid_notional * (dca_w[0] / total_w)
        qty         = t1_notional / curr_p if curr_p > 0 else 0.0
        if qty <= 0:
            continue

        dca_targets = _build_dca_targets(curr_p, trigger_side, grid_notional, regime=_btc_regime)

        atr = atr_from_ohlcv(ohlcv_1m[-15:], period=10)
        cd_map[symbol] = now_ts + OPEN_SYMBOL_COOLDOWN_SEC

        # 추세 방향 태깅 (역방향 MR 추적용)
        if ema_20_5m > 0 and ema_20_15m > 0:
            _trend_tag = "UP" if ema_20_5m > ema_20_15m * 1.002 else ("DOWN" if ema_20_5m < ema_20_15m * 0.998 else "FLAT")
        else:
            _trend_tag = "FLAT"
        reason = reason + f"_TREND({_trend_tag})"

        intents.append(Intent(
            trace_id=_tid(),
            intent_type=IntentType.OPEN,
            symbol=symbol,
            side=trigger_side,
            qty=qty,
            price=curr_p,
            reason=reason,
            metadata={
                "atr":              atr,
                "dca_targets":      dca_targets,
                "positionSide":     "LONG" if trigger_side == "buy" else "SHORT",
                "entry_type":       entry_type_tag,
                "role":             "CORE_MR" if entry_type_tag == "MR" else "CORE_BREAKOUT",
                # ★ v10.6: 진입 시 레짐 잠금 (이후 좁아지지 않음)
                "locked_regime":    _btc_regime,
            },
        ))

    return intents


# ═════════════════════════════════════════════════════════════════
# DCA Planner
# ═════════════════════════════════════════════════════════════════
def plan_dca(
    snapshot: MarketSnapshot,
    st: Dict,
    cooldowns: Dict,
    system_state: Dict = None,
) -> List[Intent]:
    intents: List[Intent] = []
    now = time.time()

    # ★ v10.20 → v10.21: 동적 DCA 제한 — 방향별 분리
    # 시장 전체 방향성 이동(동시 DCA 다수 / HARD_SL 반복) 감지 시 DCA 레벨 자동 축소
    _dyn_now = now
    # (1) 현재 DCA 진행 중 포지션 수 — 방향별 분리
    _in_dca_long = _in_dca_short = 0
    for _, p2 in _pos_items(st):
        if int(p2.get("dca_level", 1) or 1) >= 2 and p2.get("role", "") not in _HEDGE_ROLES_SLOT:
            if p2.get("side", "") == "buy":
                _in_dca_long += 1
            else:
                _in_dca_short += 1

    def _conc_max(cnt):
        return 5 if cnt < 2 else (3 if cnt < 3 else 2)

    # (2) 최근 2시간 HARD_SL 발생 수 — 방향별 분리
    _hsl_cutoff = _dyn_now - 7200
    _hsl_raw = (system_state or {}).get("_hard_sl_history", [])
    # 하위호환: 기존 float list → dict list 마이그레이션
    _hsl_hist = []
    for e in (_hsl_raw or []):
        if isinstance(e, dict):
            if e.get("ts", 0) > _hsl_cutoff:
                _hsl_hist.append(e)
        elif isinstance(e, (int, float)):
            if e > _hsl_cutoff:
                _hsl_hist.append({"ts": e, "side": "buy"})  # 레거시: 방향 불명 → buy 기본
    if system_state is not None:
        system_state["_hard_sl_history"] = _hsl_hist
    _hsl_long  = sum(1 for e in _hsl_hist if e.get("side") == "buy")
    _hsl_short = sum(1 for e in _hsl_hist if e.get("side") == "sell")

    def _hsl_max(cnt):
        return 5 if cnt == 0 else (3 if cnt < 2 else 2)

    # 방향별 동적 max tier
    _dyn_max_long  = min(_conc_max(_in_dca_long),  _hsl_max(_hsl_long))
    _dyn_max_short = min(_conc_max(_in_dca_short), _hsl_max(_hsl_short))
    if _dyn_max_long < 5 or _dyn_max_short < 5:
        print(f"[DCA_LIMIT] L=T{_dyn_max_long}(dca={_in_dca_long},sl={_hsl_long}) "
              f"S=T{_dyn_max_short}(dca={_in_dca_short},sl={_hsl_short})")

    # ★ v10.12: BTC Crash Filter — 롱 DCA만 차단 (2중 감지)
    _btc_pool_dca = (snapshot.ohlcv_pool or {}).get("BTC/USDT", {})
    _btc_1m_dca = _btc_pool_dca.get("1m", [])
    _btc_crash_dca = False
    if len(_btc_1m_dca) >= 4:
        _btc_now = float(_btc_1m_dca[-1][4])
        _btc_1ago = float(_btc_1m_dca[-2][4])
        _btc_3ago = float(_btc_1m_dca[-4][4])
        # 1분 -0.5% OR 3분 -0.8%
        if _btc_1ago > 0 and (_btc_now - _btc_1ago) / _btc_1ago <= BTC_CRASH_1M_THRESHOLD:
            _btc_crash_dca = True
        elif _btc_3ago > 0 and (_btc_now - _btc_3ago) / _btc_3ago <= BTC_CRASH_3M_THRESHOLD:
            _btc_crash_dca = True

    # ★ v10.13: killswitch 감지 (보험 시그널용)
    _killswitch_dca = float(getattr(snapshot, "margin_ratio", 0.0) or 0.0) >= 0.8

    # ★ V10.18: DCA에도 Slot Balance Gate 적용 (루프 밖 1회 계산)
    _dca_longs, _dca_shorts = _count_active_by_side(st)

    for symbol, p in _pos_items(st):

        # ★ v10.11: CORE_HEDGE 수익 중이면 DCA 금지 (보험→본체 방지)
        if is_hedge_dca_blocked(p, snapshot, symbol):
            continue

        # ★ V10.18 Rule A: 반대=0 AND 이쪽≥3 → DCA도 차단 (skew 악화 방지)
        _dca_side = p.get("side", "")
        if p.get("role", "") not in _HEDGE_ROLES_SLOT:
            if _dca_side == "buy" and _dca_shorts == 0 and _dca_longs >= 3:
                continue
            if _dca_side == "sell" and _dca_longs == 0 and _dca_shorts >= 3:
                continue

        # ★ v10.8: DCA 하드가드
        # 1) dca_level >= 5이면 스킵
        if int(p.get("dca_level", 1) or 1) >= 5:
            continue
        # 2) max_dca_reached 플래그 (dca_level 리셋 버그 방어)
        if p.get("max_dca_reached"):
            continue
        # 3) 노셔널이 grid의 95% 이상이면 스킵
        _total_cap_dca = float(getattr(snapshot, "real_balance_usdt", 0.0) or 0.0)
        if _total_cap_dca > 0:
            _grid_max = (_total_cap_dca / GRID_DIVISOR) * LEVERAGE
            _cur_notional = float(p.get("amt", 0)) * float(p.get("ep", 0))
            if _cur_notional >= _grid_max * 0.95:
                continue

        # pending_dca 타임아웃: 3분 초과 시 자동 해제
        # ★ v10.14: pending limit가 아직 활성이면 해제하지 않음 (중복 DCA 방지)
        pd = p.get("pending_dca")
        if pd:
            pd_ts = pd.get("ts", 0) if isinstance(pd, dict) else 0
            pd_tier = pd.get("tier", 0) if isinstance(pd, dict) else 0
            if time.time() - pd_ts < 180:
                continue
            # 180초 초과 — pending limit 레지스트리 확인
            _has_pending_limit = False
            try:
                from v9.execution.order_router import get_pending_limits
                for _pl_info in get_pending_limits().values():
                    if (_pl_info.get("sym") == symbol and _pl_info.get("side") == p.get("side", "")
                            and _pl_info.get("intent_type") == "DCA"
                            and _pl_info.get("tier") == pd_tier):
                        _has_pending_limit = True
                        break
            except Exception:
                pass
            if _has_pending_limit:
                continue  # limit 주문 아직 활성 → pending_dca 유지
            p["pending_dca"] = None

        dca_targets = p.get("dca_targets", [])
        if not dca_targets:
            # ★ v10.11b: dca_targets 비어있으면 자동 재생성 (hedge→MR 전환 포지션 방어)
            _rb_ep = float(p.get("ep", 0) or 0)
            _rb_side = p.get("side", "buy")
            _rb_amt = float(p.get("amt", 0) or 0)
            _rb_dca = int(p.get("dca_level", 1) or 1)
            if _rb_ep > 0 and _rb_amt > 0 and _rb_dca < 5:
                _rb_notional = _rb_ep * _rb_amt
                # ★ v10.11b: 누적 weight로 grid 추정 (dca_level까지 소화한 비중)
                _cum_w = sum(DCA_WEIGHTS[:_rb_dca]) if _rb_dca <= len(DCA_WEIGHTS) else sum(DCA_WEIGHTS)
                _total_w = sum(DCA_WEIGHTS)
                _rb_grid = _rb_notional / (_cum_w / _total_w) if _cum_w > 0 else _rb_notional * 5
                _rb_regime = p.get("locked_regime", "LOW")
                _all_targets = _build_dca_targets(_rb_ep, _rb_side, _rb_grid, _rb_regime)
                # 이미 완료된 tier 제외
                p["dca_targets"] = [t for t in _all_targets if t.get("tier", 0) > _rb_dca]
                dca_targets = p["dca_targets"]
                print(f"[DCA] {symbol} dca_targets 자동 재생성: {len(dca_targets)}개 (dca_level={_rb_dca}, T{_rb_dca+1}부터, grid=${_rb_grid:.0f})")
            if not dca_targets:
                continue

        curr_p = float((snapshot.all_prices or {}).get(symbol, 0.0))
        if curr_p <= 0:
            continue

        # ★ v10.2: 소스 포지션에 HEDGE가 붙어있으면 T3/T4 DCA 차단
        # 동일 심볼 반대방향에 role=HEDGE 포지션이 있는지 확인
        _sym_st = st.get(symbol, {})
        _opp_side = "sell" if p.get("side") == "buy" else "buy"
        _opp_p = get_p(_sym_st, _opp_side)
        _has_hedge = isinstance(_opp_p, dict) and _opp_p.get("role") == "HEDGE"

        is_long       = p.get("side", "") == "buy"
        # ★ v9.9: 티어별 쿨다운 — 루프 내부(tier_now 기준)에서 계산 (PATCH BUG3)
        time_since_last = now - p.get("last_dca_time", p.get("time", now))
        # ★ v10.13: 쿨다운 early-exit 제거 → ROI 도달 후 보험 시그널용으로 사용

        # ★ v10.13: corr 체크를 DCA 진입 직전으로 이동 (보험 플래그는 corr 무관)
        corr = (snapshot.correlations or {}).get(symbol, 1.0)

        pool      = (snapshot.ohlcv_pool or {}).get(symbol, {})
        ohlcv_1m  = pool.get("1m", [])
        closes_1m = [float(x[4]) for x in ohlcv_1m] if ohlcv_1m else []
        rsi_now   = calc_rsi(closes_1m,      period=14) if len(closes_1m) >= 15 else 50.0
        rsi_prev  = calc_rsi(closes_1m[:-1], period=14) if len(closes_1m) >= 16 else 50.0

        # ★ v10.1: Pullback DCA2 전용 — EMA20_5m 실시간 계산
        ohlcv_5m_dca  = pool.get("5m", [])
        ema20_5m_live = 0.0
        if len(ohlcv_5m_dca) >= 21:
            closes_5m_dca = [float(c[4]) for c in ohlcv_5m_dca]
            ema20_5m_live = calc_ema(closes_5m_dca, period=20)

        # ★ v10.11b: 현재 평단(ep) 기준 ROI — DCA 체결 후 평단 기준으로 다음 DCA 판단
        _ref_ep = float(p.get("ep", 0.0) or 0.0)
        roi_now = calc_roi_pct(_ref_ep, curr_p, p.get("side", ""), LEVERAGE) if _ref_ep > 0 else 0.0

        for target in dca_targets:
            # avg_ep 기준 ROI 트리거 (T2:-3.5% / T3:-5.0% / T4:-5.5%)
            roi_trig = DCA_ROI_TRIGGERS.get(target.get("tier", 2), -8.25)  # ★ PATCH: 항상 config 기준 (저장값 무시 → 런타임 변경 즉시 반영)

            _curr_dca_check = int(p.get("dca_level", 1) or 1)

            # ★ TP_LOCK: 잠긴 T1 → T2 강제 DCA (ROI 무시, 즉시 불타기)
            if p.get("tp_lock_force_dca") and _curr_dca_check == 1 and target.get("tier") == 2:
                roi_trig = 999.0  # 무조건 통과
                print(f"[TP_LOCK_DCA] {symbol} T1→T2 강제 DCA (완충재 승격)")

            is_hit = roi_now <= roi_trig
            if not is_hit:
                continue

            tier_now = target.get("tier", 2)
            # ★ PATCH BUG3: 루프 내부에서 tier_now 기준 쿨다운 계산
            tier_cooldown = DCA_COOLDOWN_BY_TIER.get(tier_now, DCA_COOLDOWN_SEC)

            # ★ v10.11b: 이미 완료된 tier 스킵 (JSON 잔여 타겟 방어)
            _curr_dca_level = int(p.get("dca_level", 1) or 1)
            if tier_now <= _curr_dca_level:
                continue

            # ★ TP_LOCK force DCA 판정 (블록/필터 바이패스용)
            _is_force_dca = p.get("tp_lock_force_dca") and _curr_dca_level == 1 and tier_now == 2

            # ★ v10.13: DCA 조건 도달 — 차단 여부 확인 → 보험 시그널
            # plan_dca에서 직접 판단 (기존 _scan_dca_blocked_insurance 제거)
            # ep 기준 ROI 불일치 버그 원천 해결
            _block = None
            if _is_force_dca:
                pass  # ★ TP_LOCK force DCA: 쿨다운/크래시 바이패스
            # ★ v10.21: 동적 DCA 레벨 제한 — 방향별 적용
            elif is_long and tier_now > _dyn_max_long:
                _block = f"DCA_LIMIT_L_T{_dyn_max_long}"
            elif not is_long and tier_now > _dyn_max_short:
                _block = f"DCA_LIMIT_S_T{_dyn_max_short}"
            elif _btc_crash_dca and is_long:
                _block = "BTC_CRASH"
            elif _killswitch_dca:
                _block = "KILLSWITCH"
            elif time_since_last < tier_cooldown:
                # ★ PATCH BUG3: T3+도 보험 트리거 대상
                # 기존: T3+ else break → DCA도 없고 보험도 없이 통과 → HARD_SL 직행
                # 수정: 최소 60초 경과 후 쿨다운 차단 시 보험 발동 (tier 무관)
                _min_elapsed = 60 if _curr_dca_level >= 3 else 120
                if time_since_last >= _min_elapsed:
                    _block = "COOLDOWN"
                else:
                    continue  # 60초도 안 됨 → 이번 틱 skip (break 아님, 다음 tier 시도)
            if _block:
                if not p.get("insurance_sh_trigger"):
                    p["insurance_sh_trigger"] = _block
                    print(f"[INSURANCE] {symbol} DCA T{tier_now} ROI hit "
                          f"(ep={_ref_ep:.4f} roi={roi_now:.2f}%) "
                          f"but blocked ({_block})")
                break  # 플래그 세팅 완료 → 다음 심볼

            # ── 차단 아님 → 품질 필터 + DCA 진입 ──
            # ★ v10.14c: corr 부족도 보험 트리거 (기존: 보험 없이 DCA만 스킵 → SL 직행)
            # ★ TP_LOCK force_dca는 품질 필터 바이패스
            if not _is_force_dca and corr < DCA_MIN_CORR:
                if not p.get("insurance_sh_trigger"):
                    p["insurance_sh_trigger"] = "CORR_LOW"
                    print(f"[INSURANCE] {symbol} DCA T{tier_now} corr={corr:.2f}<{DCA_MIN_CORR} → 보험")
                break
            if _has_hedge and tier_now >= 3:
                continue
            # ★ v10.8: SH 소스는 DCA 허용 — DCA 체결 시 SH trailing 전환
            # BALANCE는 CORE와 동일하게 T1~T4 전체 DCA 허용
            # T2/T3: RSI 느슨한 필터 (≤45 or hook≤45) / T4: 무조건 통과
            if tier_now >= 4:
                rsi_ok = True
            elif _is_force_dca:
                rsi_ok = True  # ★ TP_LOCK force DCA는 RSI 무시
            elif is_long:
                rsi_ok = (rsi_now <= 45) or (rsi_now > rsi_prev and rsi_prev <= 45)
            else:
                rsi_ok = (rsi_now >= 55) or (rsi_now < rsi_prev and rsi_prev >= 55)
            if not rsi_ok:
                continue

            qty = target["notional"] / curr_p if curr_p > 0 else 0.0
            if qty <= 0:
                continue

            MIN_NOTIONAL = 20.0
            if qty * curr_p < MIN_NOTIONAL:
                if target.get("tier") == 4:
                    p["dca_level"] = 4
                continue

            # 동일 tier pending_dca 중복 차단
            _pend_existing = p.get("pending_dca") or {}
            if _pend_existing.get("tier") == target["tier"]:
                continue
            p["pending_dca"] = {"tier": target["tier"], "ts": time.time()}

            # ★ v10.8: T5 체결가 기록 — Hard Hedge 진입/HARD_SL 기준점
            if target["tier"] == 5:
                p["t5_entry_price"] = curr_p
            # ★ v10.11b: T4 체결가 기록 — T4 전용 TP 기준점
            if target["tier"] == 4:
                p["t4_entry_price"] = curr_p

            # ★ v10.15: HIGH 레짐에서 DCA는 시장가 (체결 지연 방지)
            _dca_regime = _btc_vol_regime(snapshot)
            _force_mkt = (_dca_regime == "HIGH")

            intents.append(Intent(
                trace_id=_tid(),
                intent_type=IntentType.DCA,
                symbol=symbol,
                side=p.get("side", ""),
                qty=qty,
                price=curr_p,
                reason=f"DCA_T{target['tier']}",
                metadata={"tier": target["tier"], "target": target,
                          "locked_regime": _dca_regime,
                          "_expected_role": p.get("role", "CORE_MR"),
                          "force_market": _force_mkt},
            ))

            # ★ TP_LOCK: 강제 DCA 완료 → 플래그 클리어
            if p.get("tp_lock_force_dca") and target["tier"] == 2:
                p["tp_lock_force_dca"] = False
                print(f"[TP_LOCK_DCA] {symbol} T1→T2 승격 완료")
            # ★ v10.9: ML 피처 로깅
            try:
                from v9.logging.logger_ml import (
                    log_ml_features, calc_btc_returns, calc_skew, calc_vol_ratio_5m
                )
                _ml_regime = _btc_vol_regime(snapshot)
                _ml_btc5, _ml_btc15, _ml_btc1h = calc_btc_returns(snapshot)
                _ml_skew, _ml_skew_side = calc_skew(st, float(getattr(snapshot, "real_balance_usdt", 0) or 0), LEVERAGE)
                _ml_atr_1m = atr_from_ohlcv(ohlcv_1m[-15:], period=10) if len(ohlcv_1m) >= 15 else 0.0
                _ml_atr_5m = atr_from_ohlcv(ohlcv_5m_dca[-15:], period=10) if len(ohlcv_5m_dca) >= 15 else 0.0
                _ml_rsi5 = calc_rsi([float(c[4]) for c in ohlcv_5m_dca], period=14) if len(ohlcv_5m_dca) >= 15 else 50.0
                _ml_ema15 = calc_ema([float(c[4]) for c in pool.get("15m", [])], period=20) if len(pool.get("15m", [])) >= 20 else 0.0
                log_ml_features(
                    trace_id=intents[-1].trace_id,
                    event_type=f"DCA_T{target['tier']}",
                    symbol=symbol, side=p.get("side", ""), dca_level=target["tier"],
                    regime=_ml_regime, ema_pctl=_regime_ema_pctl or 0.0,
                    atr_pctl_raw=0.0, atr_5m_pct=_ml_atr_5m/curr_p if curr_p>0 else 0,
                    atr_1m_pct=_ml_atr_1m/curr_p if curr_p>0 else 0,
                    skew=_ml_skew, skew_side=_ml_skew_side,
                    rsi_5m=_ml_rsi5, rsi_1m=rsi_now,
                    btc_ret_5m=_ml_btc5, btc_ret_15m=_ml_btc15, btc_ret_1h=_ml_btc1h,
                    curr_roi=roi_now, max_roi_seen=float(p.get("max_roi_seen", 0) or 0),
                    hold_sec=time.time()-float(p.get("time", time.time()) or time.time()),
                    vol_ratio_5m=calc_vol_ratio_5m(ohlcv_5m_dca),
                    src_ep=_ref_ep, curr_p=curr_p, ema20_15m=_ml_ema15, ema20_5m=ema20_5m_live,
                )
            except Exception as _ml_e:
                print(f"[ML_LOG] DCA 피처 기록 오류(무시): {_ml_e}")
            break

    return intents


# ★ v10.17: Heavy TP 로그 중복 방지 — (symbol, side) 당 1회만 출력
_heavy_tp_logged: set = set()  # 현재 skew 구간에서 이미 로그된 슬롯

# ═════════════════════════════════════════════════════════════════
# TP Lock — 마진 불균형 시 light side 익절 잠금 (★ v10.16)
# ★ v10.17: (locked, heavy_side, skew) tuple 반환
#            thresh_2 → SKEW_STAGE2_TRIGGER(15%)로 하향 조정
#            CORE_HEDGE 활성 시 lock_count 최대 1개로 제한
# ═════════════════════════════════════════════════════════════════
_tp_lock_active = False   # 히스테리시스 상태

def _calc_tp_lock(snapshot: MarketSnapshot, st: Dict):
    """마진 불균형 시 light side 상위 슬롯의 (symbol, side) set 반환.

    Returns: (locked: set, heavy_side: str, skew: float)
      - locked: TP1/TP2를 스킵해야 하는 (symbol, side) 집합
      - heavy_side: "buy" | "sell" | "" (스큐 없으면 빈 문자열)
      - skew: 현재 스큐값 (0.0 ~ 1.0)
    """
    global _tp_lock_active

    total_cap = float(getattr(snapshot, "real_balance_usdt", 0) or 0)
    if total_cap <= 0:
        return set(), "", 0.0

    skew, long_m, short_m = calc_skew(st, total_cap)

    # ── 히스테리시스: 이미 활성이면 RELEASE 기준, 비활성이면 SKEW_1 기준 ──
    if not _tp_lock_active and skew < TP_LOCK_SKEW_1:
        return set(), "", skew
    if _tp_lock_active and skew < TP_LOCK_RELEASE:
        _tp_lock_active = False
        _heavy_tp_logged.clear()  # ★ v10.17: skew 해제 시 로그 쿨다운 리셋
        print(f"[TP_LOCK] OFF — skew={skew:.3f} < release={TP_LOCK_RELEASE}")
        return set(), "", skew

    _tp_lock_active = True

    heavy_side = "buy" if long_m > short_m else "sell"
    light_side = "sell" if heavy_side == "buy" else "buy"

    # ── Heavy side 스트레스 감지 ──
    heavy_stressed = False
    for sym, sym_st in st.items():
        hp = get_p(sym_st, heavy_side)
        if not isinstance(hp, dict):
            continue
        cp = float((snapshot.all_prices or {}).get(sym, 0.0))
        if cp <= 0:
            continue
        roi = calc_roi_pct(float(hp.get("ep", 0)), cp, heavy_side, LEVERAGE)
        if roi <= TP_LOCK_STRESS_ROI:
            heavy_stressed = True
            break

    stress_mult = TP_LOCK_STRESS_MULT if heavy_stressed else 1.0
    thresh_1 = TP_LOCK_SKEW_1 * stress_mult
    # ★ v10.17: thresh_2 = SKEW_STAGE2_TRIGGER(15%) — 기존 TP_LOCK_SKEW_2(20%)에서 하향
    thresh_2 = SKEW_STAGE2_TRIGGER * stress_mult

    if skew >= thresh_2:
        lock_count = 2
    elif skew >= thresh_1:
        lock_count = 1
    else:
        # 히스테리시스 유지 중이지만 스트레스 보정 후 미달
        return set(), heavy_side, skew

    # ★ v10.17: CORE_HEDGE 활성 시 lock_count 최대 1개 (헷지가 이미 offset 역할)
    _HEDGE_ROLES = {"CORE_HEDGE", "INSURANCE_SH", "HEDGE", "SOFT_HEDGE"}
    _hedge_active = any(
        isinstance(get_p(sym_st, s), dict)
        and (get_p(sym_st, s) or {}).get("role") == "CORE_HEDGE"
        for sym, sym_st in st.items() if isinstance(sym_st, dict)
        for s in ("buy", "sell")
    )
    if _hedge_active:
        lock_count = min(lock_count, 1)

    # ── Light side 후보: 미실현 PnL 내림차순 ──
    candidates = []   # (symbol, side, unrealized_pnl, roi)
    light_total = 0

    for sym, sym_st in st.items():
        lp = get_p(sym_st, light_side)
        if not isinstance(lp, dict):
            continue
        if lp.get("role", "") in _HEDGE_ROLES:
            continue
        light_total += 1
        cp = float((snapshot.all_prices or {}).get(sym, 0.0))
        if cp <= 0:
            continue
        amt = float(lp.get("amt", 0))
        ep  = float(lp.get("ep", 0))
        roi = calc_roi_pct(ep, cp, light_side, LEVERAGE)
        # ROI < 최소 기준 → 잠금 제외 (역전 리스크)
        if roi < TP_LOCK_MIN_ROI:
            continue
        # 수익 소진 해제 (ROI < EXIT 기준)
        if roi < TP_LOCK_EXIT_ROI:
            continue
        # 미실현 PnL 계산 (절대금액)
        if light_side == "buy":
            pnl = amt * (cp - ep)
        else:
            pnl = amt * (ep - cp)
        candidates.append((sym, light_side, pnl, roi))

    # ── 안전장치: light side 전부 잠금 금지 ──
    lock_count = min(lock_count, max(0, light_total - 1))
    if lock_count <= 0:
        return set(), heavy_side, skew

    # PnL 내림차순 정렬 → 상위 N개 잠금
    candidates.sort(key=lambda x: -x[2])
    locked = set()
    for i, (sym, side, pnl, roi) in enumerate(candidates):
        if i >= lock_count:
            break
        locked.add((sym, side))
        print(f"[TP_LOCK] {sym} {side} LOCKED — pnl=${pnl:.1f} roi={roi:.1f}% "
              f"(skew={skew:.3f} stage={'2' if skew>=SKEW_STAGE2_TRIGGER else '1'} "
              f"stress={'Y' if heavy_stressed else 'N'} hedge_active={'Y' if _hedge_active else 'N'})")

    return locked, heavy_side, skew


# ═════════════════════════════════════════════════════════════════
# ★ V10.21: 양방향 동시 TP — 스큐 중립 수익 확정
# 롱/숏 모두 ROI ≥ 2% 슬롯이 있으면 1:1 매칭 동시 TP1
# TP_LOCK 활성 시 비활성 (스큐 완화 목적과 충돌 방지)
# ═════════════════════════════════════════════════════════════════
BILATERAL_TP_MIN_ROI = {1: 2.0, 2: 1.5, 3: 1.0, 4: 1.0, 5: 1.0}
BILATERAL_TP_CD_SEC  = 120  # 2분 쿨다운
_bilateral_tp_cd     = 0.0


def plan_bilateral_tp(snapshot: MarketSnapshot, st: Dict,
                      exclude_syms: set = None) -> List[Intent]:
    """양방향 ROI ≥ 2% 슬롯 1:1 매칭 동시 TP1 (40% 부분익절)."""
    global _bilateral_tp_cd
    intents: List[Intent] = []
    now = time.time()
    if now < _bilateral_tp_cd:
        return intents

    # TP_LOCK 활성 시 비활성
    if _tp_lock_active:
        return intents

    prices = snapshot.all_prices or {}
    _excl = exclude_syms or set()

    # 롱/숏 후보 수집 — ROI 높은 순
    long_cands = []   # (sym, p, roi, cp)
    short_cands = []
    for sym, sym_st in st.items():
        if not isinstance(sym_st, dict) or sym in _excl:
            continue
        for pos_side, p in iter_positions(sym_st):
            if not isinstance(p, dict):
                continue
            if p.get("step", 0) != 0 or p.get("tp1_done"):
                continue
            if p.get("pending_close"):
                continue
            if p.get("tp_locked"):
                continue
            if p.get("role", "") in _HEDGE_ROLES_SLOT:
                continue
            cp = float(prices.get(sym, 0) or 0)
            ep = float(p.get("ep", 0) or 0)
            if cp <= 0 or ep <= 0:
                continue
            roi = calc_roi_pct(ep, cp, pos_side, LEVERAGE)
            dca = int(p.get("dca_level", 1) or 1)
            min_roi = BILATERAL_TP_MIN_ROI.get(dca, 2.0)
            if roi < min_roi:
                continue
            if pos_side == "buy":
                long_cands.append((sym, p, roi, cp))
            else:
                short_cands.append((sym, p, roi, cp))

    if not long_cands or not short_cands:
        return intents

    # ROI 높은 순 정렬 → 1:1 매칭
    long_cands.sort(key=lambda x: -x[2])
    short_cands.sort(key=lambda x: -x[2])
    pairs = min(len(long_cands), len(short_cands))

    for i in range(pairs):
        for (sym, p, roi, cp), close_dir in [
            (long_cands[i], "sell"),
            (short_cands[i], "buy"),
        ]:
            total_qty = float(p.get("amt", 0.0))
            close_qty = total_qty * TP1_PARTIAL_RATIO
            _sym_min_qty = {
                "ETH/USDT": 0.001, "BNB/USDT": 0.01, "SOL/USDT": 0.1,
                "BTC/USDT": 0.001, "AVAX/USDT": 0.1,
            }.get(sym, 1.0)
            if close_qty < _sym_min_qty:
                close_qty = total_qty
            if close_qty <= 0:
                continue
            dca = int(p.get("dca_level", 1) or 1)
            intents.append(Intent(
                trace_id=_tid(),
                intent_type=IntentType.FORCE_CLOSE,
                symbol=sym, side=close_dir,
                qty=close_qty, price=cp,
                reason=f"BILATERAL_TP(roi={roi:+.1f}%)_T{dca}",
                metadata={"roi_pct": roi, "_expected_role": p.get("role", ""),
                           "bilateral": True},
            ))
        print(f"[BILATERAL_TP] L:{long_cands[i][0]} roi={long_cands[i][2]:+.1f}% "
              f"↔ S:{short_cands[i][0]} roi={short_cands[i][2]:+.1f}%")

    if intents:
        _bilateral_tp_cd = now + BILATERAL_TP_CD_SEC

    return intents


# ═════════════════════════════════════════════════════════════════
# TP1 Planner
# ═════════════════════════════════════════════════════════════════
def plan_tp1(snapshot: MarketSnapshot, st: Dict,
             exclude_syms: set = None) -> List[Intent]:
    # ★ v10.16: TP Lock — 마진 불균형 시 light side 익절 잠금
    # ★ v10.17: (locked, heavy_side, skew) tuple 언패킹
    _tp_locked, _heavy_side, _cur_skew = _calc_tp_lock(snapshot, st)

    intents: List[Intent] = []
    _tp1_excl = exclude_syms or set()
    # ★ V10.18: Slot Balance 루프 밖 1회 캐싱
    _tp1_longs, _tp1_shorts = _count_active_by_side(st)
    for symbol, p in _pos_items(st):
        if symbol in _tp1_excl:
            continue
        if p.get("step", 0) != 0 or p.get("tp1_done"):
            continue
        # ★ v10.16: TP Lock 가드 (light side 완전 블록)
        if (symbol, p.get("side", "")) in _tp_locked:
            continue
        # ★ v10.8: pending 주문 있으면 스킵 (TP1 295회 반복 방지)
        if p.get("pending_close"):
            continue
        # ★ v10.13: TP1 선주문 활성이면 plan_tp1 스킵 (runner가 관리)
        if p.get("tp1_preorder_id"):
            continue
        # ★ V10.16 FIX: exit_fail_cooldown 체크 (-2022 무한반복 방지)
        _sym_st_tp1 = st.get(symbol, {})
        if float(_sym_st_tp1.get("exit_fail_cooldown_until", 0.0) or 0.0) > time.time():
            continue

        # ★ V10.17 Rule B: Light side 마지막 슬롯 보호 — TP1 차단
        if p.get("role", "") not in _HEDGE_ROLES_SLOT:
            _lb_side = p.get("side", "")
            if _lb_side == "buy" and _tp1_longs <= 1 and _tp1_shorts >= 2:
                continue
            if _lb_side == "sell" and _tp1_shorts <= 1 and _tp1_longs >= 2:
                continue

        # ★ V10.16: TP_LOCK — 잠긴 포지션은 TP1 보류
        if p.get("tp_locked"):
            curr_p_chk = float((snapshot.all_prices or {}).get(symbol, 0.0))
            if curr_p_chk > 0:
                _lock_roi = calc_roi_pct(p.get("ep", 0.0), curr_p_chk, p.get("side", ""), LEVERAGE)
                print(f"[TP1_BLOCKED] {symbol} {p.get('side','')} roi={_lock_roi:.1f}% reason=TP_LOCK")
            continue

        curr_p = float((snapshot.all_prices or {}).get(symbol, 0.0))
        if curr_p <= 0:
            continue

        is_long   = p.get("side", "") == "buy"
        _role     = p.get("role", "")
        dca_level = int(p.get("dca_level", 1) or 1)

        # ★ v10.8: HEDGE/SOFT_HEDGE → hedge_engine_v2 위임
        # ★ v10.10: INSURANCE_SH → timecut 전용, TP1 없음
        # ★ v10.11: CORE_HEDGE → 부분익절 없음, hedge_core 모듈에서 관리
        if _role == "INSURANCE_SH":
            continue
        if _role == "CORE_HEDGE":
            continue
        if _role in ("HEDGE", "SOFT_HEDGE"):
            from v9.engines.hedge_engine_v2 import check_hedge_tp1
            _tp1_ok, roi_gross, tp1_thresh = check_hedge_tp1(p, curr_p)
            if not _tp1_ok:
                continue
            # ★ v10.8: SH는 부분익절 없이 100% trailing 전환
            p["step"] = 1
            p["tp1_done"] = True
            p["trailing_on_time"] = time.time()
            print(f"[SOFT_HEDGE] {symbol} TP1 {roi_gross:.1f}% → 100% trailing")
            continue
        else:
            # ★ v10.14c: min_roi 반등 TP1
            # current_roi(ep 기준) ≥ worst_roi + α → TP1 발동
            # T1/T2: α=2.0 (넉넉히, 기존 고정TP와 유사)
            # T3~T5: α=1.5 (DCA 깊은 구간, 바닥 대비 빠른 탈출)
            # ★ PATCH: t5_mini_alpha 있으면 우선 사용 (미니게임 alpha=1.0)
            roi_gross = calc_roi_pct(p.get("ep", 0.0), curr_p, p.get("side", ""), LEVERAGE)

            _alpha = float(p.get("t5_mini_alpha") or 0) or REBOUND_ALPHA.get(dca_level, 2.0)
            _worst = float(p.get("worst_roi", 0.0) or 0.0)
            tp1_thresh = min(_worst + _alpha, _alpha)

            # ★ FLOOR: T1~T3만 손실 TP1 방지 / T4~T5는 약손실 탈출 허용
            if dca_level <= 3:
                tp1_thresh = max(tp1_thresh, 0.3)

        # ★ v10.17: Heavy side 조기 TP — 스큐 시 REBOUND_ALPHA 완화
        # light side는 위에서 이미 블록됨. heavy side 슬롯만 이 경로 진입.
        if (_heavy_side and _cur_skew >= TP_LOCK_SKEW_1
                and p.get("side", "") == _heavy_side
                and not (_bad_mode_active and dca_level == 1)):  # BAD T1 기준은 유지
            _early_roi = (SKEW_HEAVY_TP_ROI_2 if _cur_skew >= SKEW_STAGE2_TRIGGER
                          else SKEW_HEAVY_TP_ROI_1)
            if tp1_thresh > _early_roi:
                tp1_thresh = _early_roi  # 완화 (올리지 않음)
                # ★ v10.17: (symbol, side) 당 1회만 출력
                _log_key = (symbol, p.get("side", ""))
                if _log_key not in _heavy_tp_logged:
                    _heavy_tp_logged.add(_log_key)
                    print(f"[HEAVY_TP] {symbol} {p.get('side','')} 조기TP 기준 완화: "
                          f"thresh={tp1_thresh:.1f}% (skew={_cur_skew:.3f})")

        if roi_gross >= tp1_thresh:
            total_qty  = float(p.get("amt", 0.0))
            close_qty  = total_qty * TP1_PARTIAL_RATIO
            _sym_min_qty = {
                "ETH/USDT": 0.001, "BNB/USDT": 0.01, "SOL/USDT": 0.1,
                "BTC/USDT": 0.001, "AVAX/USDT": 0.1,
            }.get(symbol, 1.0)
            if close_qty < _sym_min_qty:
                close_qty = total_qty
            if close_qty <= 0:
                continue
            intents.append(Intent(
                trace_id=_tid(),
                intent_type=IntentType.TP1,
                symbol=symbol,
                side="sell" if is_long else "buy",
                qty=close_qty,
                price=curr_p,
                reason=f"TP1_RB(w={_worst:.1f}+a={_alpha:.1f}→{tp1_thresh:.1f},roi={roi_gross:.1f})_T{dca_level}",
                metadata={"roi_gross": roi_gross, "worst_roi": _worst, "alpha": _alpha,
                           "tp1_thresh": tp1_thresh},
            ))
    return intents


# ═════════════════════════════════════════════════════════════════
# TP2 Planner
# ═════════════════════════════════════════════════════════════════
def plan_tp2(snapshot: MarketSnapshot, st: Dict) -> List[Intent]:
    # ★ v10.16: TP Lock 재사용 / ★ v10.17: tuple 언패킹
    _tp_locked, _heavy_side, _cur_skew = _calc_tp_lock(snapshot, st)

    intents: List[Intent] = []
    for symbol, p in _pos_items(st):
        if p.get("step", 0) != 1 or p.get("tp2_done"):
            continue
        # ★ v10.16: TP Lock 가드
        if (symbol, p.get("side", "")) in _tp_locked:
            continue

        curr_p = float((snapshot.all_prices or {}).get(symbol, 0.0))
        if curr_p <= 0:
            continue

        is_long = p.get("side", "") == "buy"
        roi_net = calc_roi_pct_net(p.get("ep", 0.0), curr_p, p.get("side", ""), LEVERAGE)
        if roi_net >= TP2_PCT:
            total_qty  = float(p.get("amt", 0.0))
            close_qty  = total_qty * TP2_PARTIAL_RATIO
            # ★ v10.5 fix: 부분청산 qty < 최소수량 시 전량청산
            _sym_min_qty = {
                "ETH/USDT": 0.001, "BNB/USDT": 0.01, "SOL/USDT": 0.1,
                "BTC/USDT": 0.001, "AVAX/USDT": 0.1,
            }.get(symbol, 1.0)
            if close_qty < _sym_min_qty:
                close_qty = total_qty
            if close_qty <= 0:
                continue
            intents.append(Intent(
                trace_id=_tid(),
                intent_type=IntentType.TP2,
                symbol=symbol,
                side="sell" if is_long else "buy",
                qty=close_qty,
                price=curr_p,
                reason=f"TP2_{TP2_PCT:.1f}pct_net",
                metadata={"roi_net": roi_net, "is_tp2": True},
            ))
    return intents


# ═════════════════════════════════════════════════════════════════
# TRAIL ON Planner
# ═════════════════════════════════════════════════════════════════
def plan_trail_on(snapshot: MarketSnapshot, st: Dict) -> List[Intent]:
    intents: List[Intent] = []
    now = time.time()

    for symbol, sym_st in st.items():
        if not isinstance(sym_st, dict):
            continue
        for _iter_side, p in iter_positions(sym_st):
            # ★ v10.10 fix: iter_positions side를 dict에 강제 주입
            if isinstance(p, dict):
                p["side"] = _iter_side
            curr_p = float((snapshot.all_prices or {}).get(symbol, 0.0))
            if curr_p <= 0:
                continue

            is_long = p.get("side", "") == "buy"

            # ★ [BUG-SH3] side 교차검증 — 이제 항상 일치하지만 방어적 유지
            _expected_side = "buy" if _iter_side == "buy" else "sell"
            if p.get("side", "") != _expected_side:
                print(f"[TRAIL_GUARD] {symbol} side 불일치! "
                      f"slot={_iter_side} p.side={p.get('side')} → 스킵")
                continue

            # ★ V10.16 FIX: exit_fail_cooldown 체크 (-2022 무한반복 방지)
            if float(sym_st.get("exit_fail_cooldown_until", 0.0) or 0.0) > now:
                continue

            # ★ FIX: 이미 청산 주문 진행 중이면 스킵 (TRAIL_ON 30건/5분 스팸 방지)
            if p.get("pending_close"):
                continue

            # ★ V10.18 Rule B: Light side 마지막 슬롯 — trailing 청산도 차단
            if p.get("role", "") not in _HEDGE_ROLES_SLOT:
                _tr_longs, _tr_shorts = _count_active_by_side(st)
                if _iter_side == "buy" and _tr_longs <= 1 and _tr_shorts >= 2:
                    continue
                if _iter_side == "sell" and _tr_shorts <= 1 and _tr_longs >= 2:
                    continue

            roi_pct = calc_roi_pct(p.get("ep", 0.0), curr_p, p.get("side", ""), LEVERAGE)
            max_roi = p.get("max_roi_seen", roi_pct)
            if roi_pct > max_roi:
                p["max_roi_seen"] = roi_pct
                continue

            # ── MR/Pullback 통합 트레일링 (step >= 1 필요) ─────────────
            step = p.get("step", 0)
            if step < 1:
                continue

            # ──
            pool     = (snapshot.ohlcv_pool or {}).get(symbol, {})
            ohlcv_1m = pool.get("1m", [])
            atr_live = atr_from_ohlcv(ohlcv_1m[-15:], period=10) if len(ohlcv_1m) >= 15 else 0.0
            atr_val  = atr_live if atr_live > 0 else p.get("atr", 0.0)
            atr_ratio = (
                ((atr_val / curr_p) / HARD_SL_ATR_BASE)
                if (atr_val > 0 and curr_p > 0) else 1.0
            )
            # ATR 보정 계수 (0.7 ~ 1.3)
            atr_mult = max(0.7, min(1.3, math.pow(atr_ratio, 0.3)))

            # 구간별 트레일링 (ATR 보정 적용) + Profit Cap
            # ★ v10.8: DCA 깊을수록 타이트하게
            dca_level = int(p.get("dca_level", 1) or 1)
            _is_hedge_trail = p.get("role") in ("HEDGE", "SOFT_HEDGE")
            if _is_hedge_trail:
                _trail_squeeze = 0.7
            else:
                _trail_squeeze = 0.3  # ★ v10.12: 전 tier 0.3 (백테스트 검증: +$764)

            trailing_triggered = False
            trail_reason       = "TRAILING_STOP"

            # ★ V10.16: FIXED gap trail (bt_trail 검증: PROG +747 → FIXED +820)
            # max_roi 올라가면 손절선도 같은 간격으로 따라감
            # gap=0.3%: max=2% → stop=1.7% / max=5% → stop=4.7%
            FIXED_TRAIL_GAP = 0.3
            _stop = max_roi - FIXED_TRAIL_GAP
            if roi_pct <= _stop:
                trailing_triggered = True
                trail_reason = f"FTRAIL_{FIXED_TRAIL_GAP}(max={max_roi:.1f},stop={_stop:.2f})"

            # TP1 하한선 컷
            if p.get("role") in ("HEDGE", "SOFT_HEDGE"):
                tp1_floor = 0.2
                # ★ v10.8: SH 즉시 trailing → 진입 후 120초 유예 (floor 즉사 방지)
                _trail_ts = p.get("trailing_on_time") or now
                if p.get("role") == "SOFT_HEDGE" and (now - _trail_ts) < 120:
                    tp1_floor = -1.0  # 유예 중 -1%까지 허용
            else:
                # ★ v10.12: entry floor 제거 (백테스트: FL 0.0/0.3/0.5 동일 결과)
                # squeeze 0.3이 항상 먼저 발동하므로 floor 불필요
                if dca_level >= 5:
                    _eff_tp = 1.5
                    tp1_floor = max(0.3, _eff_tp * 0.5)
                else:
                    tp1_floor = 0.1  # ep 기준 최소 마지노선만 유지
            if roi_pct <= tp1_floor:
                trailing_triggered = True
                trail_reason = f"TP1_FLOOR_{tp1_floor:.1f}PCT"

            # ATR 기반 동적 타임컷
            # ★ v10.9: SH는 max 15분, CORE/HH는 45~120분
            if p.get("role") == "SOFT_HEDGE":
                dyn_timeout_sec = min(900, max(300, int(300 * math.sqrt(atr_ratio))))
            else:
                dyn_timeout_min = max(45, min(120, int(TRAILING_TIMEOUT_MIN * math.sqrt(atr_ratio))))
                dyn_timeout_sec = dyn_timeout_min * 60
            trailing_on_time = p.get("trailing_on_time")
            if trailing_on_time and (now - trailing_on_time) >= dyn_timeout_sec:
                trailing_triggered = True
                trail_reason = f"TRAILING_TIMEOUT_{dyn_timeout_sec//60}m"

            if trailing_triggered:
                intents.append(Intent(
                    trace_id=_tid(),
                    intent_type=IntentType.TRAIL_ON,
                    symbol=symbol,
                    side="sell" if is_long else "buy",
                    qty=float(p.get("amt", 0.0)),
                    price=curr_p,
                    reason=trail_reason,
                    metadata={
                        "roi_pct": roi_pct,
                        "max_roi": max_roi,
                        "tp1_floor": tp1_floor,
                        # ★ [BUG-SH3] strategy_core에서 role 교차검증용
                        "_expected_role": p.get("role", ""),
                    },
                ))

    return intents


# ═════════════════════════════════════════════════════════════════
# Force Close  (HARD_SL + DD Shutdown + ZOMBIE)
# ═════════════════════════════════════════════════════════════════
# ── V10.17: ZOMBIE 재구성 + 배치 익절 ────────────────────────────
ZOMBIE_ROI_THRESH  = -5.0
ZOMBIE_COOLDOWN_SEC = 8 * 3600
ZOMBIE_BATCH_TP_ROI = {1: 4.0, 2: 2.0}
_zombie_cooldown = {"buy": 0.0, "sell": 0.0}


def _zombie_exit(p: dict, roi_pct: float, now: float, atr_pct: float = 0.0, snapshot=None) -> tuple:
    """V10.17 ZOMBIE — BAD 레짐 + 슬롯풀 + T2+ + ROI≤-5%."""
    _role = p.get("role", "")
    if _role in ("HEDGE", "SOFT_HEDGE", "INSURANCE_SH", "CORE_HEDGE"):
        return False, ""
    dca_level = int(p.get("dca_level", 1) or 1)
    if dca_level < 2:
        return False, ""
    if not _bad_regime_active:
        return False, ""
    if roi_pct > ZOMBIE_ROI_THRESH:
        return False, ""
    _side = p.get("side", "buy")
    if now < _zombie_cooldown.get(_side, 0.0):
        return False, ""
    return True, f"ZOMBIE_T{dca_level}_roi{roi_pct:.1f}%"


def plan_force_close(
    snapshot: MarketSnapshot,
    st: Dict,
    system_state: Dict,
) -> List[Intent]:
    intents: List[Intent] = []
    shutdown_active = system_state.get("shutdown_active", False)
    now = time.time()
    _closing_set: set = set()  # 이미 청산 intent 생성된 (symbol, side) 추적

    for symbol, p in _pos_items(st):

        curr_p = float((snapshot.all_prices or {}).get(symbol, 0.0))
        if curr_p <= 0:
            continue

        is_long = p.get("side", "") == "buy"
        roi_pct = calc_roi_pct(p.get("ep", 0.0), curr_p, p.get("side", ""), LEVERAGE)

        force  = False
        reason = ""

        # ★ v10.12: 잔량 정리 — notional < $5이면 즉시 market 청산
        _res_amt = float(p.get("amt", 0.0) or 0.0)
        _res_notional = _res_amt * curr_p
        if 0 < _res_notional < 5.0:
            force  = True
            reason = f"RESIDUAL_CLEANUP(${_res_notional:.2f})"

        # ★ v10.10: DD_SHUTDOWN — 출혈 중인 CORE만 청산
        # HEDGE/INSURANCE_SH: 폭락 방어 중 → 살려야 함
        # trailing(step>=1): 수익 확정 중 → 살려야 함
        if shutdown_active:
            _dd_role = p.get("role", "")
            _dd_step = int(p.get("step", 0) or 0)
            if _dd_role in ("HEDGE", "SOFT_HEDGE", "INSURANCE_SH"):
                pass  # 보호 포지션 — DD에서 제외
            elif _dd_step >= 1:
                pass  # trailing 중 — DD에서 제외
            else:
                force  = True
                reason = "DD_SHUTDOWN_FORCE_CLOSE"

        # ★ v10.8: HEDGE/SOFT_HEDGE → hedge_engine_v2 위임
        elif p.get("role") in ("HEDGE", "SOFT_HEDGE"):
            if (symbol, p.get("side", "")) in _closing_set:
                continue
            from v9.engines.hedge_engine_v2 import plan_hedge_exit
            _h_force, _h_reason, _h_extra = plan_hedge_exit(
                symbol, p, curr_p, roi_pct, st, snapshot, _closing_set
            )
            intents.extend(_h_extra)
            if _h_force:
                force  = True
                reason = _h_reason

        # ★ V10.16: INSURANCE_SH — 60초 타임컷 + 수익 기반 분기
        elif p.get("role") == "INSURANCE_SH":
            _ins_time = float(p.get("time", now) or now)
            _ins_age = now - _ins_time

            # 60초 경과 시 수익/손실에 따라 분기
            if not force and _ins_age >= 60:
                if roi_pct > 0:
                    # ★ 수익 → trailing 전환 (plan_trail_on이 관리)
                    p["step"] = 1
                    p["tp1_done"] = True
                    p["trailing_on_time"] = now
                    p["max_roi_seen"] = max(float(p.get("max_roi_seen", 0) or 0), roi_pct)
                    print(f"[INSURANCE_SH] {symbol} 60s roi={roi_pct:+.1f}% → trailing 전환")
                    continue  # force close 하지 않음 — trailing으로 위임
                else:
                    force  = True
                    reason = f"INSURANCE_SH_TIMECUT(60s,roi={roi_pct:+.1f}%)"

        else:
            # ── HARD_SL (CORE 포지션 전용) ────────────────────────
            pool      = (snapshot.ohlcv_pool or {}).get(symbol, {})
            _ohlcv_1m = pool.get("1m", [])
            _atr      = atr_from_ohlcv(_ohlcv_1m[-15:], period=10) if len(_ohlcv_1m) >= 15 else 0.0
            _atr_mult = (_atr / curr_p) / HARD_SL_ATR_BASE if (curr_p > 0 and _atr > 0) else 1.0
            _factor   = max(HARD_SL_FACTOR_MIN, min(HARD_SL_FACTOR_MAX, _atr_mult))

            # ★ v10.12: HARD_SL — entry 기준 -5.0% 통일 (T1~T4), T5 독립 -2.0%
            _dca_lv_sl = int(p.get("dca_level", 1) or 1)

            # ★ PATCH: 미니게임 SL — 시작 가격 기준 -0.5% (ROI -1.5%)
            if p.get("t5_mini_active"):
                _sl_thresh = -1.5
                _sl_ep = float(p.get("t5_mini_start_price", 0.0) or 0.0)
                if _sl_ep <= 0:
                    _sl_ep = float(p.get("ep", 0.0))
            elif _dca_lv_sl >= 5:
                # ★ 최종 아키텍처: T5 SL -10.0% ROI
                _sl_thresh = -10.0
                _sl_ep = float(p.get("t5_entry_price", 0.0) or 0.0)
                if _sl_ep <= 0:
                    _sl_ep = float(p.get("original_ep", 0.0) or p.get("ep", 0.0))
            else:
                # ★ 최종 아키텍처: T1~T4 SL -11.2% ROI
                _sl_thresh = -11.2
                _entry_keys = {2: "t2_entry_price", 3: "t3_entry_price", 4: "t4_entry_price"}
                _sl_ep = float(p.get(_entry_keys.get(_dca_lv_sl, ""), 0.0) or 0.0)
                if _sl_ep <= 0:
                    # ★ V10.16 FIX: original_ep 폴백 → ep(평단) 폴백
                    # 재시작/sync로 tier entry price 유실 시 original_ep(T1가)로 계산하면
                    # DCA 후 평단이 낮아진 것을 반영하지 못해 조기 SL 발동
                    _sl_ep = float(p.get("ep", 0.0) or 0.0)

            if _sl_ep > 0:
                _sl_roi = calc_roi_pct(_sl_ep, curr_p, p.get("side", ""), LEVERAGE)
                if _sl_roi <= _sl_thresh:
                    force  = True
                    reason = f"HARD_SL_T{_dca_lv_sl}({_sl_thresh}%,roi={_sl_roi:.1f}%)"
                    # ★ v10.21: HARD_SL 발생 기록 — 방향별 (plan_dca 동적 제한)
                    _hsl = system_state.setdefault("_hard_sl_history", [])
                    _hsl.append({"ts": time.time(), "side": p.get("side", "buy")})

            # ★ V10.17: ZOMBIE — BAD + 슬롯풀 + T2+ + ROI≤-5%
            if not force:
                _z_side = p.get("side", "buy")
                _z_slots = count_slots(st)
                _z_full = (_z_slots.risk_long >= MAX_LONG if _z_side == "buy" else _z_slots.risk_short >= MAX_SHORT)
                if _z_full:
                    _zf, _zr = _zombie_exit(p, roi_pct, now, snapshot=snapshot)
                    if _zf:
                        force  = True
                        reason = _zr
                        _zombie_cooldown[_z_side] = now + ZOMBIE_COOLDOWN_SEC

        if force:
            intents.append(Intent(
                trace_id=_tid(),
                intent_type=IntentType.FORCE_CLOSE,
                symbol=symbol,
                side="sell" if is_long else "buy",
                qty=float(p.get("amt", 0.0)),
                price=curr_p,
                reason=reason,
                metadata={"roi_pct": roi_pct, "_expected_role": p.get("role", "")},
            ))
            # ★ V10.17: 배치 익절 — 좀비킬 시 같은 방향 수익 동반 청산
            if "ZOMBIE" in reason:
                _batch_side = p.get("side", "buy")
                _batch_best = None
                _batch_best_roi = -999.0
                prices_b = snapshot.all_prices or {}
                for _b_sym, _b_st in st.items():
                    if not isinstance(_b_st, dict) or _b_sym == symbol:
                        continue
                    _b_p = get_p(_b_st, _batch_side)
                    if not isinstance(_b_p, dict):
                        continue
                    if _b_p.get("step", 0) >= 1:
                        continue
                    if _b_p.get("role", "") in ("HEDGE", "SOFT_HEDGE", "INSURANCE_SH", "CORE_HEDGE"):
                        continue
                    _b_dca = int(_b_p.get("dca_level", 1) or 1)
                    _b_cp = float(prices_b.get(_b_sym, 0) or 0)
                    _b_ep = float(_b_p.get("ep", 0) or 0)
                    if _b_cp <= 0 or _b_ep <= 0:
                        continue
                    _b_roi = calc_roi_pct(_b_ep, _b_cp, _batch_side, LEVERAGE)
                    _b_thresh = ZOMBIE_BATCH_TP_ROI.get(_b_dca, 999.0)
                    if _b_roi >= _b_thresh and _b_roi > _batch_best_roi:
                        _batch_best = (_b_sym, _b_p, _b_roi, _b_cp)
                        _batch_best_roi = _b_roi
                if _batch_best:
                    _bs, _bp, _br, _bcp = _batch_best
                    _b_close_side = "sell" if _batch_side == "buy" else "buy"
                    intents.append(Intent(
                        trace_id=_tid(),
                        intent_type=IntentType.FORCE_CLOSE,
                        symbol=_bs, side=_b_close_side,
                        qty=float(_bp.get("amt", 0.0)), price=_bcp,
                        reason=f"ZOMBIE_BATCH_TP(roi={_br:+.1f}%)",
                        metadata={"roi_pct": _br, "_expected_role": _bp.get("role", "")},
                    ))
                    print(f"[ZOMBIE_BATCH] {_bs} 동반 익절 roi={_br:+.1f}%")

    return intents


# ═════════════════════════════════════════════════════════════════
# MR Kill → ASYM 전환 플래너  (★ v9.9)
# ═════════════════════════════════════════════════════════════════
# DCA 차단 보험 (★ v10.10)
# ═════════════════════════════════════════════════════════════════

def _scan_dca_blocked_insurance(
    snapshot: MarketSnapshot,
    st: Dict,
    system_state: Dict,
) -> None:
    """
    ★ v10.13: 제거됨 — plan_dca에서 직접 insurance_sh_trigger 세팅.
    기존 버그: original_ep 기준 ROI ≠ plan_dca의 ep 기준 ROI → 오발동.
    이 함수는 호환성을 위해 no-op으로 유지 (generate_all_intents 호출부 방어).
    """
    pass


def plan_insurance_sh(
    snapshot: MarketSnapshot,
    st: Dict,
    system_state: Dict,
) -> List[Intent]:
    """
    DCA 차단 보험 SH — 3~5분 순수 timecut.
    trailing 없음. 소스 영향 없음.
    ★ v10.11b: 같은 심볼 5분 재발동 쿨다운 + DCA 레벨당 1회 제한
    ★ v10.12: 부팅 후 300초간 발동 차단 (재시작 시 오발동 방지)
    """
    intents: List[Intent] = []
    now = time.time()

    # ★ v10.12: 부팅 후 300초간 보험 스킵
    _boot_ts = float(system_state.get("_boot_ts", 0.0) or 0.0)
    if _boot_ts > 0 and (now - _boot_ts) < 300:
        # ★ PATCH: 부팅 가드 중 trigger 클리어 제거
        # 기존: 클리어 → 300s 내 BTC_CRASH 발생 시 보험 영구 소멸
        # 수정: trigger 유지 → 300s 경과 후 다음 틱에서 발동
        # (단, V9_RECOVERED 재시작 직후 오발동 방지는 DCA 쿨다운이 담당)
        return intents

    _ins_cd = system_state.setdefault("_insurance_cooldowns", {})
    _ins_dca = system_state.setdefault("_insurance_last_dca", {})  # {sym: last_fired_dca_level}

    for sym, sym_st in st.items():
        if not isinstance(sym_st, dict):
            continue

        # ★ v10.11b: 같은 심볼 쿨다운 체크 (5분)
        if now < _ins_cd.get(sym, 0):
            # ★ v10.15: trigger 유지 (plan_dca 재세팅 방지)
            continue

        for src_side, src_p in iter_positions(sym_st):
            if not isinstance(src_p, dict):
                continue
            trigger = src_p.get("insurance_sh_trigger")
            if not trigger:
                continue
            # ★ v10.15: trigger 클리어하지 않음 (DCA 체결/포지션 청산 시만 클리어)
            # 이전: 소비 후 None → plan_dca 재세팅 → 무한루프
            # 수정: 유지 → plan_dca 가드(if not trigger)가 재세팅 방지

            # ★ v10.11b: 같은 DCA 레벨에서 이미 보험 발동했으면 스킵
            _src_dca = int(src_p.get("dca_level", 1) or 1)
            if _ins_dca.get(sym, 0) >= _src_dca:
                continue

            hedge_side = "sell" if src_side == "buy" else "buy"

            # 반대방향에 이미 포지션 있으면 스킵
            opp = get_p(sym_st, hedge_side)
            if isinstance(opp, dict):
                continue

            # ★ v10.10: 보험은 최상위 규칙 — 슬롯 무시, 마진율만 체크
            _mr_ins = float(getattr(snapshot, "margin_ratio", 0.0) or 0.0)
            if _mr_ins >= 0.90:
                continue

            src_amt = float(src_p.get("amt", 0.0))
            curr_p = float((snapshot.all_prices or {}).get(sym, 0.0))
            if src_amt <= 0 or curr_p <= 0:
                continue

            # ★ PATCH: 반대 포지션 없는 포지션은 항상 100% (헷지 없는 노출 전체 커버)
            # 이 지점까지 오면 opp 체크 통과 = 반대 포지션 확실히 없음
            # 기존 60%는 반대 포지션 있을 때를 위한 로직이었으나, 없으면 의미 없음
            qty = src_amt  # 100% always
            timecut = 60  # ★ V10.16: 전 트리거 60초 통일

            intents.append(Intent(
                trace_id=_tid(),
                intent_type=IntentType.OPEN,
                symbol=sym,
                side=hedge_side,
                qty=qty,
                price=curr_p,
                reason=f"INSURANCE_SH({trigger},src={sym})",
                metadata={
                    "atr": 0.0,
                    "dca_targets": [],
                    "role": "INSURANCE_SH",
                    "entry_type": "INSURANCE_SH",
                    "source_sym": sym,
                    "insurance_timecut": timecut,
                    "positionSide": "LONG" if hedge_side == "buy" else "SHORT",
                    "locked_regime": "LOW",
                },
            ))
            print(f"[INSURANCE_SH] {sym} {hedge_side} trigger={trigger} "
                  f"qty={qty:.4f} timecut={timecut}s")
            # ★ v10.11b: 같은 심볼 5분 재발동 차단 + DCA 레벨 기록
            _ins_cd[sym] = now + 300
            _ins_dca[sym] = _src_dca

    return intents


# ═════════════════════════════════════════════════════════════════
# ★ V10.16: TP_LOCK — 스큐 대응 TP1 보류 장치
# ═════════════════════════════════════════════════════════════════
_tp_lock_prev_count = 0  # 이전 잠금 수 (로그 중복 방지)

def _evaluate_tp_lock(snapshot: MarketSnapshot, st: Dict) -> None:
    """
    스큐 + heavy side 스트레스 시 light side 승자의 TP1을 보류.
    - 발동: skew >= thresh AND heavy_total_roi <= -3.0%
    - 해제: skew <= release_thresh (단계별 히스테리시스)
    - 잠금 대상: light side 포지션 중 노셔널 큰 순 (최대 3개)
    """
    global _tp_lock_prev_count
    from v9.config import (
        TP_LOCK_SKEW_1, TP_LOCK_SKEW_2, TP_LOCK_SKEW_3,
        TP_LOCK_RELEASE_1, TP_LOCK_RELEASE_2, TP_LOCK_RELEASE_3,
        TP_LOCK_HEAVY_ROI, TP_LOCK_HEAVY_ROI_2, TP_LOCK_MAX,
    )
    from v9.engines.hedge_core import calc_skew

    total_cap = float(getattr(snapshot, "real_balance_usdt", 0.0) or 0.0)
    skew_val, long_m, short_m = calc_skew(st, total_cap)
    heavy_side = "buy" if long_m > short_m else "sell"
    light_side = "sell" if heavy_side == "buy" else "buy"

    # ── heavy side 총합 ROI 계산 ──
    prices = snapshot.all_prices or {}
    heavy_total_roi = 0.0
    heavy_count = 0
    for sym, sym_st in st.items():
        if not isinstance(sym_st, dict):
            continue
        p = get_p(sym_st, heavy_side)
        if not isinstance(p, dict):
            continue
        if p.get("role", "") in ("HEDGE", "SOFT_HEDGE", "INSURANCE_SH", "CORE_HEDGE"):
            continue
        cp = float(prices.get(sym, 0.0) or 0.0)
        ep = float(p.get("ep", 0.0) or 0.0)
        if cp > 0 and ep > 0:
            heavy_total_roi += calc_roi_pct(ep, cp, heavy_side, LEVERAGE)
            heavy_count += 1

    # ── 잠금 개수 결정 (3단계) ──
    stress_on = (heavy_total_roi <= TP_LOCK_HEAVY_ROI) if heavy_count > 0 else False

    _skew_thresholds = [TP_LOCK_SKEW_1, TP_LOCK_SKEW_2, TP_LOCK_SKEW_3]
    target_lock = 0
    if stress_on:
        for i, th in enumerate(_skew_thresholds[:TP_LOCK_MAX], 1):
            if skew_val >= th:
                target_lock = i

    # ★ V10.17: 2차 — heavy_roi ≤ -4%면 skew 무관 최소 1개 잠금
    if heavy_count > 0 and heavy_total_roi <= TP_LOCK_HEAVY_ROI_2:
        target_lock = max(target_lock, 1)

    # ── 해제 체크 (히스테리시스) ──
    currently_locked = []
    for sym, sym_st in st.items():
        if not isinstance(sym_st, dict):
            continue
        for pos_side, p in iter_positions(sym_st):
            if isinstance(p, dict) and p.get("tp_locked"):
                currently_locked.append((sym, pos_side, p))

    # ★ 방향 전환 보호: 잠긴 포지션이 heavy side로 바뀌면 즉시 해제
    _side_flipped = []
    for sym, pos_side, p in currently_locked:
        if pos_side == heavy_side:  # 잠긴 애가 이제 heavy side
            p["tp_locked"] = False
            p["tp_lock_reason"] = ""
            p["tp_lock_ts"] = None
            p["tp_lock_force_dca"] = False
            _side_flipped.append(sym)
            print(f"[TP_LOCK_OFF] {sym} {pos_side} reason=SIDE_FLIPPED "
                  f"(was light, now heavy) skew={skew_val:.2f}")
    if _side_flipped:
        currently_locked = [(s,sd,p) for s,sd,p in currently_locked
                            if s not in _side_flipped]

    cur_count = len(currently_locked)

    # 단계별 해제
    _release_thresholds = [TP_LOCK_RELEASE_1, TP_LOCK_RELEASE_2, TP_LOCK_RELEASE_3]
    for i in range(cur_count, 0, -1):
        if i <= len(_release_thresholds) and skew_val <= _release_thresholds[i-1]:
            target_lock = min(target_lock, i - 1)

    # ── 잠금 대상 선정 ──
    # T2+ 우선 (이미 큰 완충재), T1만 있으면 ROI 높은 순 → 강제 DCA
    if target_lock > cur_count:
        prices = snapshot.all_prices or {}
        cand_t2plus = []  # T2 이상 → 노셔널 큰 순
        cand_t1     = []  # T1 → ROI 높은 순
        for sym, sym_st in st.items():
            if not isinstance(sym_st, dict):
                continue
            p = get_p(sym_st, light_side)
            if not isinstance(p, dict):
                continue
            if p.get("tp_locked") or p.get("tp1_done") or p.get("step", 0) >= 1:
                continue
            if p.get("role", "") in ("HEDGE", "SOFT_HEDGE", "INSURANCE_SH", "CORE_HEDGE"):
                continue
            _dca = int(p.get("dca_level", 1) or 1)
            notional = float(p.get("amt", 0) or 0) * float(p.get("ep", 0) or 0)
            cp = float(prices.get(sym, 0) or 0)
            ep = float(p.get("ep", 0) or 0)
            _roi = calc_roi_pct(ep, cp, light_side, LEVERAGE) if (cp > 0 and ep > 0) else 0.0
            if _dca >= 2:
                cand_t2plus.append((sym, p, notional, _roi))
            else:
                cand_t1.append((sym, p, notional, _roi))

        cand_t2plus.sort(key=lambda x: x[2], reverse=True)   # 노셔널 큰 순
        cand_t1.sort(key=lambda x: x[3], reverse=True)        # ROI 높은 순
        candidates = cand_t2plus + cand_t1  # T2+ 우선

        need = target_lock - cur_count
        for sym, p, nt, _roi in candidates[:need]:
            p["tp_locked"] = True
            p["tp_lock_reason"] = f"SKEW{target_lock}"
            p["tp_lock_ts"] = int(time.time())
            # ★ T1이면 T2로 강제 DCA → 완충재 승격
            if int(p.get("dca_level", 1) or 1) == 1:
                p["tp_lock_force_dca"] = True
                # ★ V10.16 FIX: dca_targets 없으면 재생성 (RECOVERED 포지션 방어)
                if not p.get("dca_targets"):
                    _fd_ep = float(p.get("ep", 0) or 0)
                    _fd_amt = float(p.get("amt", 0) or 0)
                    if _fd_ep > 0 and _fd_amt > 0:
                        _fd_grid = (_fd_ep * _fd_amt) * (sum(DCA_WEIGHTS) / DCA_WEIGHTS[0])
                        _fd_regime = p.get("locked_regime", "LOW")
                        _fd_targets = _build_dca_targets(_fd_ep, p.get("side", "buy"), _fd_grid, _fd_regime)
                        p["dca_targets"] = [t for t in _fd_targets if t.get("tier", 0) > 1]
                        print(f"[TP_LOCK_DCA] {sym} dca_targets 재생성 {len(p['dca_targets'])}개 (force_dca용)")
            print(f"[TP_LOCK_ON] {sym} {light_side} lock_n={target_lock} "
                  f"skew={skew_val:.2f} heavy_roi={heavy_total_roi:.1f}% "
                  f"dca={p.get('dca_level',1)} roi={_roi:+.1f}%"
                  f"{'→T2' if p.get('tp_lock_force_dca') else ''}")

    elif target_lock < cur_count:
        # 잠금 해제 (노셔널 작은 순으로 해제)
        currently_locked.sort(key=lambda x: float(x[2].get("amt",0))*float(x[2].get("ep",0)))
        release_n = cur_count - target_lock
        for sym, pos_side, p in currently_locked[:release_n]:
            p["tp_locked"] = False
            p["tp_lock_reason"] = ""
            p["tp_lock_ts"] = None
            p["tp_lock_force_dca"] = False  # ★ 미완료 force_dca도 클리어
            reason = "SKEW_NORMALIZED"
            print(f"[TP_LOCK_OFF] {sym} {pos_side} reason={reason} skew={skew_val:.2f}")

    _tp_lock_prev_count = target_lock


# ═════════════════════════════════════════════════════════════════
# 전체 Intent 생성
# ═════════════════════════════════════════════════════════════════
def generate_all_intents(
    snapshot: MarketSnapshot,
    st: Dict,
    cooldowns: Dict,
    system_state: Dict,
) -> List[Intent]:
    """
    ★ V10.16 실행 순서:
      0. _evaluate_tp_lock      → TP_LOCK 잠금/해제 평가
      1. plan_force_close       → HARD_SL / DD_SHUTDOWN / HEDGE_EXIT / ZOMBIE / INSURANCE_TIMECUT
      2. plan_hedge_core_manage → CORE_HEDGE 소스 연동 (청산/CORE_MR 전환)
      3. plan_tp1               → TP1 부분익절 (40%) — tp_locked 체크
      4. plan_trail_on          → 트레일링 스탑 (잔량 60%)
      5. plan_dca               → DCA 진입 + 차단 시 insurance_sh_trigger 세팅
      6. plan_insurance_sh      → DCA 차단 보험
      7. plan_open              → MR 신규 진입 + HEDGE_CORE + BALANCE fallback
    """
    import time as _time
    _snap_ts = _time.time()
    intents: List[Intent] = []

    # ★ V10.16: TP_LOCK 평가 (plan_tp1보다 먼저!)
    try:
        _evaluate_tp_lock(snapshot, st)
    except Exception as _tpl_e:
        print(f"[TP_LOCK] 평가 오류(무시): {_tpl_e}")

    _fc_intents = plan_force_close(snapshot, st, system_state)
    intents += _fc_intents
    # ★ V10.18: force_close 대상 심볼 수집 → heavy_rebalance 중복 방지
    _fc_syms = {i.symbol for i in _fc_intents}
    intents += plan_heavy_rebalance(snapshot, st, exclude_syms=_fc_syms)
    intents += plan_hedge_core_manage(snapshot, st)
    # ★ V10.21: 양방향 동시 TP → plan_tp1보다 먼저 (중복 방지)
    _bt_intents = plan_bilateral_tp(snapshot, st, exclude_syms=_fc_syms)
    intents += _bt_intents
    _bt_syms = {i.symbol for i in _bt_intents}
    intents += plan_tp1(snapshot, st, exclude_syms=_bt_syms)
    intents += plan_trail_on(snapshot, st)
    intents += plan_dca(snapshot, st, cooldowns, system_state)
    intents += plan_insurance_sh(snapshot, st, system_state)
    intents += plan_open(snapshot, st, cooldowns, system_state)
    for _i in intents:
        if _i.metadata is None:
            _i.metadata = {}
        _i.metadata["snap_ts"] = _snap_ts
    return intents
