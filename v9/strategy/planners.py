"""
V9 Strategy — Planners  (v10.27 — TP1 고정값 + 스큐 시뮬 + SL 타이트)
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
    LEVERAGE, DCA_WEIGHTS, TP1_PARTIAL_RATIO,
    TRAILING_TIMEOUT_MIN,
    DCA_COOLDOWN_BY_TIER, DCA_COOLDOWN_SEC,
    TOTAL_MAX_SLOTS, MAX_LONG, MAX_SHORT, GRID_DIVISOR, MAX_MR_PER_SIDE,
    HARD_SL_ATR_BASE,  # plan_open ATR 계산용
    DCA_MIN_CORR,
    FALLING_KNIFE_BARS, FALLING_KNIFE_THRESHOLD,
    LONG_ONLY_SYMBOLS, SHORT_ONLY_SYMBOLS,
    OPEN_CORR_MIN,
    SKEW_STAGE2_TRIGGER,
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

# ★ V10.27: 방향별 글로벌 진입 쿨다운 (연타 방지)
OPEN_DIR_COOLDOWN_SEC    = 10 * 60   # 같은 방향 진입 후 10분 대기
_open_dir_cd = {"buy": 0.0, "sell": 0.0}  # {side: next_allowed_ts}

# ★ V10.27: 통합 ATR base + slot 불균형 ±패널티
_ATR_BASE = 2.4           # 롱/숏 통합
_ATR_SLOT_STEP = 0.2      # 슬롯 차이 1개당 heavy +0.2 / light -0.2




def _tid() -> str:
    return str(uuid.uuid4())[:8]


# ═════════════════════════════════════════════════════════════════
# ★ V10.26b: ATR 비율 계산 — 레짐 라벨 대체
# ═════════════════════════════════════════════════════════════════

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


# ═════════════════════════════════════════════════════════════════
# ★ V10.22: Skew-Aware TP — 통합 스큐 대응 시스템
#   5개 레거시(TP_LOCK, HEAVY_REBALANCE, Bilateral TP, Rule B, Heavy 조기 TP)를
#   단일 함수로 교체. 모든 TP 판단에 skew_mult × slot_mult 적용.
# ═════════════════════════════════════════════════════════════════
# ★ V10.25: 스큐 15%+ 지속 시간 추적 (light side 탈출구용)

def _skew_tp_adjustment(pos_side: str, st: Dict, snapshot) -> dict:
    """★ V10.27: Skew-Aware TP — heavy floor + light 무한블록 + 매도후 스큐 시뮬.

    heavy side: 스큐 비례 할인 (floor 0.5) + 10%+ full_close
    light side: 스큐 ≥ 15% → 무한 블록 (세미헷지, 탈출구 없음)
    매도후 스큐 시뮬: light 매도 시 스큐 12%+ 복귀 → 차단
    스큐 정상화 시 블록 해제 → 정상 TP1 복귀
    """
    total_cap = float(getattr(snapshot, "real_balance_usdt", 0) or 0)
    if total_cap <= 0:
        return {"skew_mult": 1.0, "slot_mult": 1.0, "full_close": False,
                "blocked": False, "skew": 0.0, "heavy_side": ""}

    skew, long_m, short_m = calc_skew(st, total_cap)
    heavy_side = "buy" if long_m > short_m else "sell"
    is_heavy = (pos_side == heavy_side)

    if skew < 0.03:
        skew_mult = 1.0
        full_close = False
        blocked = False
    elif is_heavy:
        # Heavy side: 스큐 비례 할인 (5%→0.85, 10%→0.70, 15%→0.55)
        skew_mult = max(0.40, 1.0 - skew * 3.0)
        full_close = (skew >= 0.10)
        blocked = False
    else:
        # Light side: 무한 블록 (세미헷지)
        if skew >= 0.15:
            blocked = True
        else:
            blocked = False
        skew_mult = 1.0
        full_close = False

    # ── Light side 마지막 슬롯 보호 ──
    if not blocked and not is_heavy:
        longs, shorts = _count_active_by_side(st)
        if pos_side == "buy" and longs <= 1 and shorts >= 2:
            blocked = True
        elif pos_side == "sell" and shorts <= 1 and longs >= 2:
            blocked = True

    # ═══════════════════════════════════════════════════════════
    # ★ V10.27: 매도 후 스큐 시뮬레이션
    # light 매도 시 스큐가 12%+로 복귀하면 차단
    # (DCA로 마진 올려서 스큐 해소 → 바로 익절 → 원복 무한루프 방지)
    # ═══════════════════════════════════════════════════════════
    if not blocked and not is_heavy and skew >= 0.05:
        if pos_side == "buy":
            my_margin = long_m
            opp_margin = short_m
        else:
            my_margin = short_m
            opp_margin = long_m

        my_count = sum(
            1 for s in st
            if isinstance(get_p(st.get(s, {}), pos_side), dict)
            and get_p(st.get(s, {}), pos_side).get("role", "") not in _HEDGE_ROLES_SLOT
        )
        if my_count > 0:
            avg_margin_per_slot = my_margin / my_count
            post_my_margin = my_margin - avg_margin_per_slot
            post_skew = abs(opp_margin - post_my_margin)
            if post_skew >= 0.12:
                blocked = True

    # ── Floor 보장 ──
    skew_mult = max(0.5, skew_mult)

    return {
        "skew_mult": skew_mult,
        "slot_mult": 1.0,     # ★ V10.27: slot_mult 제거 (과잉 곱셈 방지)
        "full_close": full_close,
        "blocked": blocked,
        "skew": skew,
        "heavy_side": heavy_side,
    }



# ★ V10.22: plan_heavy_rebalance 삭제 — _skew_tp_adjustment()의 full_close로 대체
_heavy_rebalance_deleted = True  # 참조 방지용 마커


# ═════════════════════════════════════════════════════════════════
# 1h 추세 필터  (EMA20 vs EMA50)
# ═════════════════════════════════════════════════════════════════
def _trend_filter_side(symbol: str, snapshot: MarketSnapshot) -> set:
    """★ V10.27c: 비활성화 — 양방향 허용."""
    return {"buy", "sell"}



# ═════════════════════════════════════════════════════════════════
# DCA 타겟 빌더
# ═════════════════════════════════════════════════════════════════
# DCA ROI 트리거 (avg_ep 기준 실시간 ROI)
# T2: -3.5% / T3: -5.0% / T4: -5.5%
DCA_ROI_TRIGGERS = {2: -3.0, 3: -5.0, 4: -7.0}  # ★ V10.27b: 좁은 DCA (빠른 평단 압축)

REGIME_HARD_SL       = {"BAD": -5.0, "LOW": -6.5, "NORMAL": -8.0, "HIGH": -10.0}
_REGIME_WIDTH = {"HIGH": 4, "NORMAL": 3, "LOW": 2, "BAD": 1}

def _wider_regime(a: str, b: str) -> str:
    return a if _REGIME_WIDTH.get(a, 0) >= _REGIME_WIDTH.get(b, 0) else b

def _build_dca_targets(
    entry_p: float, side: str, grid_notional: float,
    regime: str = "LOW",
) -> list:
    """★ V10.22: DCA 4단 타겟 — T2/T3/T4."""
    dca_w   = DCA_WEIGHTS
    total_w = sum(dca_w)
    targets = []
    for i, tier in enumerate([2, 3, 4]):
        roi_trig = DCA_ROI_TRIGGERS.get(tier, -8.0)
        dist = abs(roi_trig) / 100 / LEVERAGE
        target_p = entry_p * (1.0 - dist) if side == "buy" else entry_p * (1.0 + dist)
        notional = grid_notional * (dca_w[i + 1] / total_w)
        targets.append({"tier": tier, "target_p": target_p,
                        "notional": notional, "roi_trigger": roi_trig})
    return targets



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
    _asym_syms = set()  # hedge_core에서 사용 (중복 진입 방지)

    # ★ v10.9: 레짐 판정을 먼저 (크래시보다 선행)
    _btc_regime = _btc_vol_regime(snapshot)
    # ★ v10.13b: BAD 제거됨 — 이 조건은 더 이상 발동하지 않음

    # ★ v10.9: BTC Crash Filter — 급락 시 롱만 차단 (숏 MR은 허용)
    _btc_crash_active = _check_btc_crash(snapshot, system_state)

    _long_atr_base = _ATR_BASE   # ★ V10.27: 통합 2.4
    _short_atr_base = _ATR_BASE

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

    # ★ V10.27: 슬롯 불균형 ±패널티 — heavy +0.2 / light -0.2 per slot diff
    # L3 S1 → diff=2 → 롱 ATR=2.8(진입↓) 숏 ATR=2.0(진입↑)
    _slot_diff = _core_long - _core_short  # 양수면 롱이 heavy
    _penalty = abs(_slot_diff) * _ATR_SLOT_STEP
    if _slot_diff > 0:
        _mr_atr_mult_long  = _long_atr_base + _penalty   # heavy: 어렵게
        _mr_atr_mult_short = max(1.6, _short_atr_base - _penalty)  # light: 쉽게 (floor 1.6)
    elif _slot_diff < 0:
        _mr_atr_mult_long  = max(1.6, _long_atr_base - _penalty)
        _mr_atr_mult_short = _short_atr_base + _penalty
    else:
        _mr_atr_mult_long  = _long_atr_base
        _mr_atr_mult_short = _short_atr_base
    _dyn_atr_key = (round(_mr_atr_mult_long, 2), round(_mr_atr_mult_short, 2))
    if _slot_diff != 0:
        _prev = getattr(plan_open, '_last_dyn_atr', None)
        if _prev != _dyn_atr_key:
            print(f"[SLOT_ATR] L={_core_long} S={_core_short} diff={_slot_diff} → "
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

        # ★ V10.27c: MR 전용 (Breakout/E30 삭제)
        _mr_long_final  = mr_long_ok  and long_trig  and micro_long_ok
        _mr_short_final = mr_short_ok and short_trig and micro_short_ok

        final_long_trig  = _mr_long_final
        final_short_trig = _mr_short_final


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
                    pending_map[symbol] = {
                        "armed":       True,
                        "armed_ts_ms": last_5m_ts,
                        "side":        "sell",
                        "entry_type":  "MR",
                        "reason":      f"HF_MR_5mRSI({rsi5_now:.0f}/{adj_rsi5_ob})_ATR({atr_mult:.1f}x)",
                    }
                    continue
                else:
                    trigger_side = "sell"
                    reason = f"HF_MR_5mRSI_ATR({atr_mult:.1f}x)"

            if final_long_trig and trigger_side is None:
                if OPEN_WAIT_NEXT_BAR:
                    pending_map[symbol] = {
                        "armed":       True,
                        "armed_ts_ms": last_5m_ts,
                        "side":        "buy",
                        "entry_type":  "MR",
                        "reason":      f"HF_MR_5mRSI({rsi5_now:.0f}/{adj_rsi5_os})_ATR({atr_mult:.1f}x)",
                    }
                    continue
                else:
                    trigger_side = "buy"
                    reason = f"HF_MR_5mRSI_ATR({atr_mult:.1f}x)"

        if trigger_side is None:
            continue

        # ★ V10.27: 방향별 글로벌 진입 쿨다운 (연타 방지)
        if now_ts < _open_dir_cd.get(trigger_side, 0.0):
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

        # ── entry_type 확정 — V10.27c: MR 전용 ──
        if _pend_entry_type is not None:
            entry_type_tag = _pend_entry_type
        else:
            entry_type_tag = "MR"
        _pend_entry_type = None

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
                # ★ V10.26: E30/MR_E30도 CORE_MR (CORE_BREAKOUT 제거)
                "role":             "CORE_MR",
                # ★ v10.6: 진입 시 레짐 잠금 (이후 좁아지지 않음)
                "locked_regime":    _btc_regime,
            },
        ))
        # ★ V10.27: 방향별 글로벌 쿨다운 기록
        _open_dir_cd[trigger_side] = now_ts + OPEN_DIR_COOLDOWN_SEC

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
        return 4 if cnt < 2 else (3 if cnt < 3 else 2)

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
        return 4 if cnt == 0 else (3 if cnt < 2 else 2)

    # 방향별 동적 max tier
    _dyn_max_long  = min(_conc_max(_in_dca_long),  _hsl_max(_hsl_long))
    _dyn_max_short = min(_conc_max(_in_dca_short), _hsl_max(_hsl_short))
    if _dyn_max_long < 4 or _dyn_max_short < 4:
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

    # ★ V10.25: 스큐 ≥ 15% → light side DCA T2 상한 (헷지 대용)
    _total_cap_skew = float(getattr(snapshot, "real_balance_usdt", 0.0) or 0.0)
    _dca_skew = 0.0
    _dca_heavy_side = ""
    if _total_cap_skew > 0:
        _dca_skew, _dca_long_m, _dca_short_m = calc_skew(st, _total_cap_skew)
        _dca_heavy_side = "buy" if _dca_long_m > _dca_short_m else "sell"

    # ★ V10.25: light side DCA 후보 ROI 정렬 (스큐 ≥ 15% 시 ROI 높은 순)
    _dca_positions = list(_pos_items(st))
    if _dca_skew >= SKEW_STAGE2_TRIGGER and _dca_heavy_side:
        _light_side = "sell" if _dca_heavy_side == "buy" else "buy"
        _prices_sort = snapshot.all_prices or {}
        def _dca_sort_key(item):
            _sym, _p = item
            if _p.get("side", "") == _light_side and _p.get("role", "") not in _HEDGE_ROLES_SLOT:
                _ep = float(_p.get("ep", 0) or 0)
                _cp = float(_prices_sort.get(_sym, 0) or 0)
                if _ep > 0 and _cp > 0:
                    return -calc_roi_pct(_ep, _cp, _light_side, LEVERAGE)  # ROI 높은 순
            return 0
        _dca_positions.sort(key=_dca_sort_key)

    for symbol, p in _dca_positions:

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

        # ★ V10.26 Rule B: 스큐 비례 light side DCA 단계적 제한
        if _dca_skew >= 0.10 and _dca_heavy_side:
            if _dca_side != _dca_heavy_side and p.get("role", "") not in _HEDGE_ROLES_SLOT:
                _cur_dca_lv = int(p.get("dca_level", 1) or 1)
                if _dca_skew >= 0.20:
                    continue  # 20%+ → light DCA 완전 차단
                elif _dca_skew >= SKEW_STAGE2_TRIGGER:
                    if _cur_dca_lv >= 2:  # 15%+ → T2 상한
                        continue
                else:
                    if _cur_dca_lv >= 3:  # 10~15% → T3 상한
                        continue

        # ★ V10.27: ATR LOW DCA 게이트 제거 (MR 전략은 횡보장에서 유리, DCA 제한 역효과)

        # ★ V10.22: DCA 하드가드 (4단)
        # 1) dca_level >= 4이면 스킵
        if int(p.get("dca_level", 1) or 1) >= 4:
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
            if _rb_ep > 0 and _rb_amt > 0 and _rb_dca < 4:
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
            roi_trig = DCA_ROI_TRIGGERS.get(target.get("tier", 2), -8.0)  # ★ PATCH: 항상 config 기준 (저장값 무시 → 런타임 변경 즉시 반영)

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

            # ★ v10.13: DCA 조건 도달 — 차단 여부 확인 → 보험 시그널
            _block = None
            # ★ v10.21: 동적 DCA 레벨 제한 — 방향별 적용
            if is_long and tier_now > _dyn_max_long:
                _block = f"DCA_LIMIT_L_T{_dyn_max_long}"
            elif not is_long and tier_now > _dyn_max_short:
                _block = f"DCA_LIMIT_S_T{_dyn_max_short}"
            elif _btc_crash_dca and is_long:
                _block = "BTC_CRASH"
            elif _killswitch_dca:
                _block = "KILLSWITCH"
            elif time_since_last < tier_cooldown:
                # ★ V10.26: COOLDOWN 보험 제거 — 쿨다운은 기다리면 정상 DCA 진행
                # 실전 8건 W2/L6 PnL=-$10 → 노이즈 트레이딩
                continue  # 쿨다운 미만 → 이번 틱 skip
            if _block:
                print(f"[DCA_BLOCKED] {symbol} DCA T{tier_now} ROI hit "
                      f"(ep={_ref_ep:.4f} roi={roi_now:.2f}%) "
                      f"but blocked ({_block})")
                break  # 차단 → 다음 심볼

            # ── 차단 아님 → 품질 필터 + DCA 진입 ──
            # ★ V10.26: CORR_LOW 보험 제거 — corr 부족 시 DCA 자체를 스킵
            if corr < DCA_MIN_CORR:
                continue  # corr 부족 → DCA 스킵 (보험 대신 대기)
            if _has_hedge and tier_now >= 3:
                continue
            # ★ v10.8: SH 소스는 DCA 허용 — DCA 체결 시 SH trailing 전환
            # BALANCE는 CORE와 동일하게 T1~T4 전체 DCA 허용
            # T2/T3: RSI 느슨한 필터 (≤45 or hook≤45) / T4: 무조건 통과
            if tier_now >= 4:
                rsi_ok = True
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

            # ★ V10.22: T4 체결가 기록 (최종 단계)
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

            # ★ v10.9: ML 피처 로깅
            try:
                from v9.logging.logger_ml import (
                    log_ml_features, calc_btc_returns,
                    calc_skew as _calc_skew_ml, calc_vol_ratio_5m
                )
                _ml_regime = _btc_vol_regime(snapshot)
                _ml_btc5, _ml_btc15, _ml_btc1h = calc_btc_returns(snapshot)
                _ml_skew, _ml_skew_side = _calc_skew_ml(st, float(getattr(snapshot, "real_balance_usdt", 0) or 0), LEVERAGE)
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



# ★ V10.22: _calc_tp_lock, plan_bilateral_tp 삭제 — _skew_tp_adjustment()로 대체


# ═════════════════════════════════════════════════════════════════
# TP1 Planner
# ═════════════════════════════════════════════════════════════════
def plan_tp1(snapshot: MarketSnapshot, st: Dict,
             exclude_syms: set = None) -> List[Intent]:
    """★ V10.27: 고정값 TP1 + skew full_close/block 판단.

    T1~T3: TP1_FIXED 고정값 × skew_mult(heavy 할인)
    T4: max(worst_roi + 2.0, 0.8) × skew_mult
    """
    intents: List[Intent] = []
    _tp1_excl = exclude_syms or set()

    for symbol, p in _pos_items(st):
        if symbol in _tp1_excl:
            continue
        if p.get("step", 0) != 0 or p.get("tp1_done"):
            continue
        if p.get("pending_close"):
            continue
        _sym_st_tp1 = st.get(symbol, {})
        if float(_sym_st_tp1.get("exit_fail_cooldown_until", 0.0) or 0.0) > time.time():
            continue

        curr_p = float((snapshot.all_prices or {}).get(symbol, 0.0))
        if curr_p <= 0:
            continue

        is_long   = p.get("side", "") == "buy"
        _role     = p.get("role", "")
        dca_level = int(p.get("dca_level", 1) or 1)

        # HEDGE 계열 → 전용 모듈 위임
        if _role == "INSURANCE_SH":
            continue
        if _role == "CORE_HEDGE":
            continue
        if _role in ("HEDGE", "SOFT_HEDGE"):
            from v9.engines.hedge_engine_v2 import check_hedge_tp1
            _tp1_ok, roi_gross, tp1_thresh = check_hedge_tp1(p, curr_p)
            if not _tp1_ok:
                continue
            p["step"] = 1
            p["tp1_done"] = True
            p["trailing_on_time"] = time.time()
            print(f"[SOFT_HEDGE] {symbol} TP1 {roi_gross:.1f}% → 100% trailing")
            continue

        # ★ V10.27: Skew — full_close/blocked 판단
        _skew = _skew_tp_adjustment(p.get("side", ""), st, snapshot)
        if _skew["blocked"]:
            continue

        roi_gross = calc_roi_pct(p.get("ep", 0.0), curr_p, p.get("side", ""), LEVERAGE)

        # ★ V10.27b: T1~T2 고정값, T3~T4 worst_roi 탈출
        from v9.config import TP1_FIXED
        if dca_level <= 2:
            tp1_base = TP1_FIXED.get(dca_level, 2.0)
        else:
            # T3/T4: worst_roi + 2.0 반등, tier별 floor
            _worst = float(p.get("worst_roi", 0.0) or 0.0)
            _floor = 0.3 if dca_level == 3 else TP1_FIXED.get(4, 0.8)
            tp1_base = max(_worst + 2.0, _floor)

        tp1_thresh = tp1_base * _skew["skew_mult"]

        if roi_gross >= tp1_thresh:
            total_qty  = float(p.get("amt", 0.0))
            if _skew["full_close"]:
                close_qty = total_qty
            else:
                close_qty = total_qty * TP1_PARTIAL_RATIO
            _sym_min_qty = {
                "ETH/USDT": 0.001, "BNB/USDT": 0.01, "SOL/USDT": 0.1,
                "BTC/USDT": 0.001, "AVAX/USDT": 0.1,
            }.get(symbol, 1.0)
            if close_qty < _sym_min_qty:
                close_qty = total_qty
            if close_qty <= 0:
                continue
            _fc = "FC" if _skew["full_close"] else "P"
            intents.append(Intent(
                trace_id=_tid(),
                intent_type=IntentType.TP1,
                symbol=symbol,
                side="sell" if is_long else "buy",
                qty=close_qty,
                price=curr_p,
                reason=(f"TP1_F({tp1_thresh:.1f},roi={roi_gross:.1f},"
                        f"sk={_skew['skew']:.2f},{_fc})_T{dca_level}"),
                metadata={"roi_gross": roi_gross, "tp1_thresh": tp1_thresh,
                           "skew": _skew["skew"], "full_close": _skew["full_close"]},
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

            # ★ V10.22: Skew-Aware light-side 보호 (trailing에도 적용)
            if p.get("role", "") not in _HEDGE_ROLES_SLOT:
                _tr_skew = _skew_tp_adjustment(_iter_side, st, snapshot)
                if _tr_skew["blocked"]:
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

            # ★ V10.27: FIXED gap trail 0.3→0.5 (noise 위킹 방지)
            FIXED_TRAIL_GAP = 0.5
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

        # ★ V10.27: INSURANCE_SH — BTC 반전 기반 청산
        elif p.get("role") == "INSURANCE_SH":
            _ins_time = float(p.get("time", now) or now)
            _ins_age = now - _ins_time

            from v9.config import (INSURANCE_TP_ROI, INSURANCE_CUT_ROI,
                                   INSURANCE_MAX_HOLD_SEC)

            # BTC 반전 감지
            btc_pool_ins = (snapshot.ohlcv_pool or {}).get("BTC/USDT", {})
            btc_1m_ins = btc_pool_ins.get("1m", [])
            _btc_reversed = False
            if len(btc_1m_ins) >= 2:
                _btc_now_ins = float(btc_1m_ins[-1][4])
                _btc_entry = float(p.get("hedge_entry_price", 0) or _btc_now_ins)
                _ins_side = p.get("side", "")
                # sell(crash 대응): BTC 올라가면 reversed
                if _ins_side == "sell" and _btc_now_ins > _btc_entry * 1.003:
                    _btc_reversed = True
                elif _ins_side == "buy" and _btc_now_ins < _btc_entry * 0.997:
                    _btc_reversed = True

            # (1) 3분 + BTC 반전 + 손실 → 위킹이었음
            if _ins_age >= 180 and _btc_reversed and roi_pct < 0:
                force = True
                reason = f"INSURANCE_SH_REVERSED({_ins_age:.0f}s,roi={roi_pct:+.1f}%)"
            # (2) 수익 3%+ → trailing
            elif roi_pct >= INSURANCE_TP_ROI:
                p["step"] = 1
                p["tp1_done"] = True
                p["trailing_on_time"] = now
                p["max_roi_seen"] = max(float(p.get("max_roi_seen", 0) or 0), roi_pct)
                print(f"[INSURANCE_SH] {symbol} roi={roi_pct:+.1f}% → trailing")
                continue
            # (3) 10분 + 손실 → 컷
            elif _ins_age >= 600 and roi_pct < INSURANCE_CUT_ROI:
                force = True
                reason = f"INSURANCE_SH_TIMECUT(10m,roi={roi_pct:+.1f}%)"
            # (4) 10분 + 수익 → trailing
            elif _ins_age >= 600 and roi_pct > 0:
                p["step"] = 1
                p["tp1_done"] = True
                p["trailing_on_time"] = now
                p["max_roi_seen"] = max(float(p.get("max_roi_seen", 0) or 0), roi_pct)
                print(f"[INSURANCE_SH] {symbol} 10m roi={roi_pct:+.1f}% → trailing")
                continue
            # (5) 20분 절대 상한
            elif _ins_age >= INSURANCE_MAX_HOLD_SEC:
                force = True
                reason = f"INSURANCE_SH_MAXTIME(20m,roi={roi_pct:+.1f}%)"

        else:
            # ── HARD_SL (CORE 포지션 전용) ────────────────────────
            # ★ V10.27c: DCA 트리거 -1% 뒤 SL / T4는 체결가 -2%
            _dca_lv_sl = int(p.get("dca_level", 1) or 1)

            from v9.config import HARD_SL_BY_TIER
            _sl_thresh = HARD_SL_BY_TIER.get(_dca_lv_sl, -4.0)

            # T1~T3: 평균 EP 기준 / T4: T4 체결가 기준
            if _dca_lv_sl >= 4:
                _sl_ep = float(p.get("t4_entry_price", 0.0) or 0.0)
                if _sl_ep <= 0:
                    _sl_ep = float(p.get("ep", 0.0) or 0.0)
            else:
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



def plan_insurance_sh(
    snapshot: MarketSnapshot,
    st: Dict,
    system_state: Dict,
) -> List[Intent]:
    """★ V10.27: BTC 급변 직접 감지 → 피해측 최약 포지션 50% 반대 헷지.

    트리거: BTC 1분 ±0.5% / 3분 ±0.8% / 5분 ±1.2%
    대상: affected side에서 ROI 최악 포지션
    사이즈: 소스 50%
    청산: plan_force_close에서 BTC 반전/시간/수익 기반 판단
    """
    intents: List[Intent] = []
    now = time.time()

    # ★ 부팅 후 300초 스킵 (기존 유지)
    _boot_ts = float(system_state.get("_boot_ts", 0.0) or 0.0)
    if _boot_ts > 0 and (now - _boot_ts) < 300:
        return intents

    # ★ 글로벌 쿨다운
    from v9.config import INSURANCE_COOLDOWN_SEC, INSURANCE_SIZE_RATIO, INSURANCE_MIN_AFFECTED
    _last_ins = float(system_state.get("_insurance_last_ts", 0.0) or 0.0)
    if now - _last_ins < INSURANCE_COOLDOWN_SEC:
        return intents

    # ★ 이미 INSURANCE_SH 포지션 활성이면 스킵
    for _s, _ss in st.items():
        if not isinstance(_ss, dict):
            continue
        for _, _pp in iter_positions(_ss):
            if isinstance(_pp, dict) and _pp.get("role") == "INSURANCE_SH":
                return intents

    # ── BTC 급변 감지 ──
    btc_pool = (snapshot.ohlcv_pool or {}).get("BTC/USDT", {})
    btc_1m = btc_pool.get("1m", [])
    if len(btc_1m) < 6:
        return intents

    now_p = float(btc_1m[-1][4])
    if now_p <= 0:
        return intents

    from v9.config import (INSURANCE_BTC_1M_THRESH, INSURANCE_BTC_3M_THRESH,
                           INSURANCE_BTC_5M_THRESH)
    checks = [
        (1, INSURANCE_BTC_1M_THRESH),
        (3, INSURANCE_BTC_3M_THRESH),
        (5, INSURANCE_BTC_5M_THRESH),
    ]

    event_detected = False
    affected_side = ""
    event_mag = 0.0
    event_bars = 0

    for bars, threshold in checks:
        if len(btc_1m) < bars + 1:
            continue
        ref_p = float(btc_1m[-(bars + 1)][4])
        if ref_p <= 0:
            continue
        ret = (now_p - ref_p) / ref_p

        if ret <= -threshold:
            event_detected = True
            affected_side = "buy"   # crash → 롱 피해
            event_mag = abs(ret)
            event_bars = bars
            break
        elif ret >= threshold:
            event_detected = True
            affected_side = "sell"  # pump → 숏 피해
            event_mag = abs(ret)
            event_bars = bars
            break

    if not event_detected:
        return intents

    # ── affected side 포지션 수 확인 ──
    affected_positions = []
    for sym, sym_st in st.items():
        if not isinstance(sym_st, dict):
            continue
        p = get_p(sym_st, affected_side)
        if not isinstance(p, dict):
            continue
        if p.get("role", "") in ("INSURANCE_SH", "CORE_HEDGE", "HEDGE", "SOFT_HEDGE"):
            continue
        cp = float((snapshot.all_prices or {}).get(sym, 0.0))
        if cp <= 0:
            continue
        ep = float(p.get("ep", 0.0) or 0.0)
        if ep <= 0:
            continue
        roi = calc_roi_pct(ep, cp, affected_side, LEVERAGE)
        affected_positions.append((sym, p, cp, roi))

    if len(affected_positions) < INSURANCE_MIN_AFFECTED:
        return intents

    # ── 최악 ROI 포지션 선택 ──
    affected_positions.sort(key=lambda x: x[3])
    worst_sym, worst_p, worst_cp, worst_roi = affected_positions[0]

    # trailing 중이면 스킵
    if int(worst_p.get("step", 0) or 0) >= 1:
        return intents

    # ── 반대방향에 이미 포지션 있으면 스킵 ──
    hedge_side = "sell" if affected_side == "buy" else "buy"
    opp = get_p(st.get(worst_sym, {}), hedge_side)
    if isinstance(opp, dict):
        return intents

    # ── MR 체크 ──
    _mr_ins = float(getattr(snapshot, "margin_ratio", 0.0) or 0.0)
    if _mr_ins >= 0.90:
        return intents

    # ── 사이즈: 소스 50% ──
    src_amt = float(worst_p.get("amt", 0.0))
    qty = src_amt * INSURANCE_SIZE_RATIO
    if qty <= 0:
        return intents

    intents.append(Intent(
        trace_id=_tid(),
        intent_type=IntentType.OPEN,
        symbol=worst_sym,
        side=hedge_side,
        qty=qty,
        price=worst_cp,
        reason=f"INSURANCE_SH(BTC_{event_bars}m_{event_mag*100:.1f}%,"
               f"src={worst_sym},roi={worst_roi:+.1f}%)",
        metadata={
            "atr": 0.0,
            "dca_targets": [],
            "role": "INSURANCE_SH",
            "entry_type": "INSURANCE_SH",
            "source_sym": worst_sym,
            "hedge_entry_price": now_p,  # BTC 가격 기록 (반전 감지용)
            "insurance_timecut": 0,  # 레거시 호환
            "positionSide": "LONG" if hedge_side == "buy" else "SHORT",
            "locked_regime": "LOW",
        },
    ))
    system_state["_insurance_last_ts"] = now
    print(f"[INSURANCE_SH] BTC {event_bars}m {event_mag*100:.1f}% → "
          f"{worst_sym} {hedge_side} (src_roi={worst_roi:+.1f}%, "
          f"qty={qty:.4f}, btc={now_p:.0f})")

    return intents



# ★ V10.22: _evaluate_tp_lock 삭제 — _skew_tp_adjustment()로 대체


# ═════════════════════════════════════════════════════════════════
# ★ V10.26: 페어 컷 — heavy 최악 + light 최선 동시 청산
# ═════════════════════════════════════════════════════════════════
_pair_cut_cooldown_ts = 0.0
_PAIR_CUT_COOLDOWN_SEC = 300  # 5분 쿨다운

def plan_pair_cut(
    snapshot: MarketSnapshot,
    st: Dict,
    system_state: Dict,
) -> List[Intent]:
    """스큐 12%+ 15분 지속 시 heavy 최악 + light 최선 페어 청산.

    조건:
      - 스큐 ≥ 12% (SKEW_HEDGE_TRIGGER)
      - 기존 stage2 타이머 15분 경과
      - heavy side에 ROI < 0 포지션 존재
      - light side에 ROI > 0 포지션 존재
      - 순 P&L > heavy 단독 HARD_SL 손실 (안전장치)
    """
    global _pair_cut_cooldown_ts
    global _pair_cut_cooldown_ts
    intents: List[Intent] = []
    now = time.time()

    if now < _pair_cut_cooldown_ts:
        return intents

    total_cap = float(getattr(snapshot, "real_balance_usdt", 0) or 0)
    if total_cap <= 0:
        return intents

    skew, long_m, short_m = calc_skew(st, total_cap)
    from v9.config import SKEW_HEDGE_TRIGGER
    if skew < SKEW_HEDGE_TRIGGER:
        return intents

    # stage2 타이머 확인 (hedge_core와 공유)
    try:
        from v9.engines.hedge_core import _skew_stage2_enter_ts
        if _skew_stage2_enter_ts <= 0:
            return intents
        _stage2_dur = now - _skew_stage2_enter_ts
    except Exception:
        _stage2_dur = 0

    # 스큐 15%+ → 15분 대기, 12~15% → 20분 대기
    _min_dur = 900 if skew >= 0.15 else 1200
    if _stage2_dur < _min_dur:
        return intents

    heavy_side = "buy" if long_m > short_m else "sell"
    light_side = "sell" if heavy_side == "buy" else "buy"
    prices = snapshot.all_prices or {}

    # ── heavy 후보: ROI 가장 낮은 포지션 ──
    heavy_candidates = []
    for sym, sym_st in st.items():
        if not isinstance(sym_st, dict):
            continue
        p = get_p(sym_st, heavy_side)
        if not isinstance(p, dict):
            continue
        if p.get("role", "") in _HEDGE_ROLES_SLOT:
            continue
        if int(p.get("step", 0) or 0) >= 1:
            continue  # trailing 중 → 건드리지 않음
        ep = float(p.get("ep", 0) or 0)
        cp = float(prices.get(sym, 0) or 0)
        if ep <= 0 or cp <= 0:
            continue
        roi = calc_roi_pct(ep, cp, heavy_side, LEVERAGE)
        if roi >= 0:
            continue  # 수익 중이면 대상 아님
        amt = float(p.get("amt", 0) or 0)
        heavy_candidates.append((sym, p, roi, amt, ep))

    if not heavy_candidates:
        return intents
    # ROI 가장 낮은 순
    heavy_candidates.sort(key=lambda x: x[2])

    # ── light 후보: ROI 가장 높은 포지션 ──
    # 마지막 슬롯 보호용 카운트 (루프 밖 1회)
    _pc_longs, _pc_shorts = _count_active_by_side(st)
    _light_cnt = _pc_longs if light_side == "buy" else _pc_shorts
    if _light_cnt <= 1:
        return intents  # light side 1개뿐 → 보호

    light_candidates = []
    for sym, sym_st in st.items():
        if not isinstance(sym_st, dict):
            continue
        p = get_p(sym_st, light_side)
        if not isinstance(p, dict):
            continue
        if p.get("role", "") in _HEDGE_ROLES_SLOT:
            continue
        ep = float(p.get("ep", 0) or 0)
        cp = float(prices.get(sym, 0) or 0)
        if ep <= 0 or cp <= 0:
            continue
        roi = calc_roi_pct(ep, cp, light_side, LEVERAGE)
        if roi <= 0:
            continue  # 손실 중이면 대상 아님
        amt = float(p.get("amt", 0) or 0)
        light_candidates.append((sym, p, roi, amt, ep))

    if not light_candidates:
        return intents
    # ROI 가장 높은 순
    light_candidates.sort(key=lambda x: -x[2])

    # ── 페어 매칭: 최악 heavy + 최선 light ──
    h_sym, h_p, h_roi, h_amt, h_ep = heavy_candidates[0]
    l_sym, l_p, l_roi, l_amt, l_ep = light_candidates[0]

    # 순 P&L 안전장치: heavy 단독 HARD_SL(-11.2%) 보다 나은지 확인
    # ★ V10.26 fix: roi는 레버리지 적용값이므로 LEVERAGE로 나눠야 실제 달러 PnL
    h_pnl = h_amt * h_ep * h_roi / (LEVERAGE * 100.0)
    l_pnl = l_amt * l_ep * l_roi / (LEVERAGE * 100.0)
    net_pnl = h_pnl + l_pnl
    hardsl_pnl = h_amt * h_ep * (-11.2) / (LEVERAGE * 100.0)

    if net_pnl <= hardsl_pnl:
        return intents  # 더 나쁘면 안 함 (사실상 거의 안 걸림)

    # ── Intent 생성 ──
    h_close_side = "sell" if heavy_side == "buy" else "buy"
    l_close_side = "sell" if light_side == "buy" else "buy"

    intents.append(Intent(
        trace_id=_tid(),
        intent_type=IntentType.FORCE_CLOSE,
        symbol=h_sym,
        side=h_close_side,
        qty=h_amt,
        price=float(prices.get(h_sym, 0)),
        reason=f"PAIR_CUT_HEAVY(roi={h_roi:+.1f}%,skew={skew*100:.0f}%,net=${net_pnl:.1f})",
        metadata={"roi_pct": h_roi, "_expected_role": h_p.get("role", ""),
                  "pair_cut": True},
    ))
    intents.append(Intent(
        trace_id=_tid(),
        intent_type=IntentType.FORCE_CLOSE,
        symbol=l_sym,
        side=l_close_side,
        qty=l_amt,
        price=float(prices.get(l_sym, 0)),
        reason=f"PAIR_CUT_LIGHT(roi={l_roi:+.1f}%,skew={skew*100:.0f}%,net=${net_pnl:.1f})",
        metadata={"roi_pct": l_roi, "_expected_role": l_p.get("role", ""),
                  "pair_cut": True},
    ))

    _pair_cut_cooldown_ts = now + _PAIR_CUT_COOLDOWN_SEC
    print(f"[PAIR_CUT] heavy={h_sym}({h_roi:+.1f}%) + light={l_sym}({l_roi:+.1f}%) "
          f"net=${net_pnl:.1f} skew={skew*100:.0f}%")

    return intents


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
    ★ V10.26 실행 순서:
      1. plan_force_close       → HARD_SL / ZOMBIE / INSURANCE_TIMECUT
      2. plan_pair_cut          → ★ 스큐 페어 컷 (heavy 최악 + light 최선)
      3. plan_hedge_core_manage → CORE_HEDGE 소스 연동
      4. plan_tp1               → TP1 Skew-Aware 부분익절
      5. plan_trail_on          → 트레일링 스탑
      6. plan_dca               → DCA 4단 진입
      7. plan_insurance_sh      → DCA 차단 보험
      8. plan_open              → MR 신규 진입 + HEDGE_CORE + BALANCE
    """
    import time as _time
    _snap_ts = _time.time()
    intents: List[Intent] = []

    _fc_intents = plan_force_close(snapshot, st, system_state)
    intents += _fc_intents
    _fc_syms = {i.symbol for i in _fc_intents}
    # ★ V10.26: 페어 컷 (force_close 직후, tp1 전)
    _pc_intents = plan_pair_cut(snapshot, st, system_state)
    intents += _pc_intents
    _fc_syms.update(i.symbol for i in _pc_intents)

    intents += plan_hedge_core_manage(snapshot, st)
    intents += plan_tp1(snapshot, st, exclude_syms=_fc_syms)
    intents += plan_trail_on(snapshot, st)
    intents += plan_dca(snapshot, st, cooldowns, system_state)
    intents += plan_insurance_sh(snapshot, st, system_state)
    intents += plan_open(snapshot, st, cooldowns, system_state)
    for _i in intents:
        if _i.metadata is None:
            _i.metadata = {}
        _i.metadata["snap_ts"] = _snap_ts
    return intents
