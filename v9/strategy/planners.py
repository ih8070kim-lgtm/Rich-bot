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
    calc_skew,
)



# ═════════════════════════════════════════════════════════════════
# ★ V10.31b: MR 가용 잔고 (BC/CB 보유 노셔널 차감)
# ═════════════════════════════════════════════════════════════════
def _mr_available_balance(snapshot, st: Dict) -> float:
    """real_balance_usdt에서 BC/CB 포지션 노셔널을 차감한 MR 가용 잔고."""
    bal = float(getattr(snapshot, "real_balance_usdt", 0.0) or 0.0)
    prices = getattr(snapshot, "all_prices", {}) or {}
    bc_notional = 0.0
    for sym, sym_st in (st or {}).items():
        if not isinstance(sym_st, dict):
            continue
        for _, p in iter_positions(sym_st):
            if not isinstance(p, dict):
                continue
            if p.get("role") in ("BC", "CB"):
                amt = float(p.get("amt", 0) or 0)
                cp = float(prices.get(sym, 0) or 0)
                if amt > 0 and cp > 0:
                    bc_notional += amt * cp
    return max(bal - bc_notional, bal * 0.3)  # 최소 30% 보장


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
    SYM_MIN_QTY, SYM_MIN_QTY_DEFAULT,
    DCA_ENTRY_BASED, DCA_ENTRY_ROI,
    DCA_ENTRY_ROI_BY_TIER,
    calc_trim_qty, calc_tp1_thresh, get_sl_entry,
)
from v9.utils.utils_math import (
    calc_rsi, calc_ema, atr_from_ohlcv, safe_float,
    calc_roi_pct, calc_roi_pct_net,
)


# ★ V10.29d: ATR shift 상수 제거 (RSI 35/65 고정)
OPEN_SYMBOL_COOLDOWN_SEC = 10 * 60
OPEN_WAIT_NEXT_BAR       = False
OPEN_PENDING_TTL_SEC     = 5 * 60

# ★ V10.27: 방향별 글로벌 진입 쿨다운 (연타 방지)
OPEN_DIR_COOLDOWN_SEC    = 10 * 60   # 같은 방향 진입 후 10분 대기
_open_dir_cd = {"buy": 0.0, "sell": 0.0}  # {side: next_allowed_ts}

# ★ V10.27: 통합 ATR base + slot 불균형 ±패널티
_ATR_BASE = 3.0           # ★ V10.29b: 백테스트 최적 (2.4→3.0)
_ATR_SLOT_STEP = 0.2      # 슬롯 차이 1개당 heavy +0.2 / light -0.2




def _tid() -> str:
    return str(uuid.uuid4())[:8]


# ═════════════════════════════════════════════════════════════════
# ★ V10.26b: ATR 비율 계산 — 레짐 라벨 대체
# ═════════════════════════════════════════════════════════════════

# ═════════════════════════════════════════════════════════════════
# ★ V10.17: Slot Balance 규칙
# ═════════════════════════════════════════════════════════════════
_HEDGE_ROLES_SLOT = {"CORE_HEDGE", "INSURANCE_SH", "HEDGE", "SOFT_HEDGE", "BC", "CB"}

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
# ★ V10.29b: T3 ROI 기반 반대편 방어 — 스큐 로직 전면 교체
# ═════════════════════════════════════════════════════════════════
# 내 사이드에 T3(-3% 이하)가 있으면 반대편 출구를 제한.
#   T1: TP ×1.5/2.0 또는 블록(-7%)
# ★ V10.29d: T3 방어 — 블록/배수 전면 제거
# ═════════════════════════════════════════════════════════════════

# ★ V10.29e: _t3_defense 제거 — TREND 진입이 스큐 해소 담당




# ★ V10.27f: Urgency Score — skew + heavy pain 통합 점수
# ═════════════════════════════════════════════════════════════════
def _calc_urgency(st: Dict, snapshot) -> dict:
    """스큐 + heavy side 평균 ROI → 단일 urgency 점수.

    urgency = skew×100 + max(0, -heavy_avg_roi)
    0~30 범위. 모니터링 전용 (V10.29e: 의사결정 로직 제거).
    """
    total_cap = float(getattr(snapshot, "real_balance_usdt", 0) or 0)
    if total_cap <= 0:
        return {"urgency": 0.0, "skew": 0.0, "heavy_avg_roi": 0.0,
                "heavy_side": "", "light_side": ""}

    skew, long_m, short_m = calc_skew(st, total_cap)
    heavy_side = "buy" if long_m > short_m else "sell"
    light_side = "sell" if heavy_side == "buy" else "buy"

    # heavy side 평균 ROI
    _HEDGE_ROLES_U = {"HEDGE", "SOFT_HEDGE", "INSURANCE_SH", "CORE_HEDGE", "BC", "CB"}
    heavy_rois = []
    prices = snapshot.all_prices or {}
    for sym, sym_st in st.items():
        if not isinstance(sym_st, dict):
            continue
        hp = get_p(sym_st, heavy_side)
        if not isinstance(hp, dict):
            continue
        if hp.get("role", "") in _HEDGE_ROLES_U:
            continue
        cp = float(prices.get(sym, 0))
        ep = float(hp.get("ep", 0))
        if cp > 0 and ep > 0:
            heavy_rois.append(calc_roi_pct(ep, cp, heavy_side, LEVERAGE))

    heavy_avg_roi = sum(heavy_rois) / len(heavy_rois) if heavy_rois else 0.0
    # ★ V10.27f: ROI 가중치 2x — 스큐 해소돼도 heavy 고통 중이면 urgency 유지
    urgency = skew * 100 + max(0.0, -heavy_avg_roi) * 2

    return {
        "urgency": urgency,
        "skew": skew,
        "heavy_avg_roi": heavy_avg_roi,
        "heavy_side": heavy_side,
        "light_side": light_side,
    }


# ═════════════════════════════════════════════════════════════════
# 1h 추세 필터  (EMA20 vs EMA50)
# ═════════════════════════════════════════════════════════════════
def _trend_filter_side(symbol: str, snapshot: MarketSnapshot) -> set:
    """★ V10.27c: 비활성화 — 양방향 허용."""
    return {"buy", "sell"}



# ═════════════════════════════════════════════════════════════════
# DCA 타겟 빌더
# ═════════════════════════════════════════════════════════════════
# DCA ROI 트리거
# ★ V10.28b: Entry 기준 -1.8% 균일 (config.DCA_ENTRY_ROI 사용)
# 레거시 호환: _build_dca_targets 사이징용으로만 사용
DCA_ROI_TRIGGERS = {2: -1.8, 3: -3.6}  # ★ V10.29b: 블렌디드 EP 기준 (바이낸스 ROI 그대로)

def _wider_regime(a: str, b: str) -> str:
    """호환용 stub — DCA 거리 통일로 항상 LOW."""
    return "LOW"

def _build_dca_targets(
    entry_p: float, side: str, grid_notional: float,
    regime: str = "LOW",
) -> list:
    """★ V10.29b: DCA 3단 타겟 — T2/T3."""
    dca_w   = DCA_WEIGHTS
    total_w = sum(dca_w)
    targets = []
    for i, tier in enumerate([2, 3]):
        roi_trig = DCA_ROI_TRIGGERS.get(tier, -8.0)
        dist = abs(roi_trig) / 100 / LEVERAGE
        target_p = entry_p * (1.0 - dist) if side == "buy" else entry_p * (1.0 + dist)
        w_idx = min(i + 1, len(dca_w) - 1)
        notional = grid_notional * (dca_w[w_idx] / total_w)
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
# ★ V10.29c: TREND COMPANION — MR 진입 시 반대 방향 추세 심볼 동시 진입
# ═════════════════════════════════════════════════════════════════
_trend_cooldown: Dict[str, float] = {}  # sym → next allowed ts

def _calc_trend_score(ohlcv_15m: list, ohlcv_1m: list = None) -> float:
    """추세 점수: EMA 이격 × 거래량 서지 × RSI 가중치.
    양수=상승 추세, 음수=하락 추세. abs() >= TREND_MIN_SCORE 이면 유효."""
    if len(ohlcv_15m) < 35:
        return 0.0
    closes = [float(b[4]) for b in ohlcv_15m]
    highs = [float(b[2]) for b in ohlcv_15m]
    lows = [float(b[3]) for b in ohlcv_15m]
    volumes = [float(b[5]) for b in ohlcv_15m]

    c = closes[-1]
    if c <= 0:
        return 0.0

    # EMA10 이격 (ATR 단위)
    k = 2 / 11; e = closes[-30]
    for v in closes[-30:]: e = v * k + e * (1 - k)
    trs = []
    for j in range(-14, 0):
        if closes[j - 1] > 0:
            trs.append(max(highs[j] - lows[j], abs(highs[j] - closes[j - 1]), abs(lows[j] - closes[j - 1])) / closes[j - 1])
    atr = (sum(trs) / len(trs) * c) if trs else 0
    if atr <= 0:
        return 0.0
    ema_dist = (c - e) / atr

    # 거래량 서지 (5봉/30봉)
    if len(volumes) >= 30:
        vf = sum(volumes[-5:]) / 5
        vs = sum(volumes[-30:]) / 30
        vol_s = vf / vs if vs > 0 else 1.0
    else:
        vol_s = 1.0

    # RSI 14
    if len(closes) >= 16:
        gains, losses = [], []
        for j in range(-14, 0):
            d = closes[j] - closes[j - 1]
            gains.append(max(d, 0)); losses.append(max(-d, 0))
        ag = sum(gains) / 14; al = sum(losses) / 14
        rsi = 100 - (100 / (1 + ag / al)) if al > 0 else 100.0
    else:
        rsi = 50.0
    rsi_w = (rsi - 50) / 50  # -1 ~ +1

    return ema_dist * vol_s * (1 + abs(rsi_w))


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
    # ★ V10.31b: 미장전 신규 진입 차단
    if system_state.get("_pmc_block_entry"):
        return intents
    long_targets  = list(getattr(snapshot, "global_targets_long",  None) or [])
    short_targets = list(getattr(snapshot, "global_targets_short", None) or [])
    # ★ Python UnboundLocalError 방지: 루프 안에서 재할당되는 변수 미리 초기화
    total_cap = _mr_available_balance(snapshot, st)  # ★ V10.31b: BC 노셔널 차감

    # ═══ V10.29d: PENDING TREND_COMP 발사 (MR fill 확인 후) ═══
    _ptc = system_state.get("_pending_trend_comp")
    if _ptc and isinstance(_ptc, dict):
        _ptc_age = time.time() - float(_ptc.get("ts", 0) or 0)
        _ptc_mr_sym = _ptc.get("mr_symbol", "")
        # MR이 실제 포지션으로 존재하는지 확인
        _ptc_mr_filled = False
        if _ptc_mr_sym:
            _ptc_mr_st = st.get(_ptc_mr_sym, {})
            for _ptc_s, _ptc_p in iter_positions(_ptc_mr_st):
                if isinstance(_ptc_p, dict) and float(_ptc_p.get("amt", 0) or 0) > 0:
                    if _ptc_p.get("role") == "CORE_MR":
                        _ptc_mr_filled = True
                        break

        if _ptc_age > 300:
            # 5분 초과 → 만료
            system_state.pop("_pending_trend_comp", None)
        elif _ptc_mr_filled:
            # MR fill 확인 → companion 발사
            _ptc_sym = _ptc["symbol"]
            _ptc_side = _ptc["side"]
            _ptc_cp = float((snapshot.all_prices or {}).get(_ptc_sym, _ptc.get("price", 0)))
            _ptc_qty = _ptc.get("qty", 0)
            if _ptc_cp > 0:
                _ptc_qty = (_ptc_qty * _ptc.get("price", _ptc_cp)) / _ptc_cp  # 현재가 기준 재계산
            if _ptc_qty > 0 and _ptc_cp > 0:
                intents.append(Intent(
                    trace_id=_tid(),
                    intent_type=IntentType.OPEN,
                    symbol=_ptc_sym,
                    side=_ptc_side,
                    qty=_ptc_qty,
                    price=_ptc_cp,
                    reason=f"TREND_COMP(sig={_ptc_mr_sym},score={_ptc.get('score',0):.1f})",
                    metadata={
                        "atr": 0,
                        "dca_targets": _ptc.get("dca_targets", []),
                        "positionSide": "LONG" if _ptc_side == "buy" else "SHORT",
                        "entry_type": "TREND",
                        "role": "CORE_MR",
                        "locked_regime": _ptc.get("regime", "LOW"),
                    },
                ))
                print(f"[TREND_FIRE] {_ptc_sym} {_ptc_side} ← MR {_ptc_mr_sym} filled "
                      f"(delay={_ptc_age:.0f}s)")
            system_state.pop("_pending_trend_comp", None)
    # ═══ END PENDING TREND_COMP ═══

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

    # ★ V10.29c: ATR 3.0 고정 (슬롯 패널티 제거)

    # ── ★ V10.29b: CORE_HEDGE 제거 — MR 전략에서 역효과 (net -16 USDT)
    # 소스 회복 시 헷지가 손실, 스큐 관리는 진입차단+TP할인으로 충분
    _skew, _long_margin, _short_margin = calc_skew(st, total_cap)


    # CORE 카운트 (FORCE_BALANCE reason용)
    _core_long = 0; _core_short = 0
    for _fb_s, _fb_ss in st.items():
        for _fb_side, _fb_pp in iter_positions(_fb_ss):
            if not isinstance(_fb_pp, dict): continue
            if _fb_pp.get("role") in ("HEDGE","SOFT_HEDGE","INSURANCE_SH","CORE_HEDGE","BC","CB"): continue
            if _fb_pp.get("step",0)>=1: continue
            if _fb_side=="buy": _core_long+=1
            else: _core_short+=1


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

    # ★ V10.29c: 슬롯 불균형 패널티 제거 — ATR 3.0 고정
    # 기존 버그: mr_short_ok가 _mr_atr_mult_long 사용 → 숏 패널티 미적용
    # 한쪽만 작동하는 패널티는 품질 악화만 초래 → 깔끔하게 제거
    _mr_atr_mult = _ATR_BASE  # 3.0 고정

    # ★ V10.18: Slot Balance 루프 밖 1회 캐싱
    _open_longs, _open_shorts = _count_active_by_side(st)

    # ★ V10.27e: EMA30 활성 슬롯 카운트 (step 무관 — 보유 중이면 카운트)
    from v9.config import MAX_E30_SLOTS
    _active_e30 = 0
    _noslot_best = None  # ★ V10.29e: TREND_NOSLOT 최고 score 후보
    for _e30_sym, _e30_p in _pos_items(st):
        if _e30_p.get("entry_type") == "15mE30":
            _active_e30 += 1

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
        # ── (1) RSI 파라미터 — ★ V10.29d: 고정 35/65 (레짐 패널티 제거)
        rsi5_os = 35
        rsi5_ob = 65

        # ── (2) 코인 ATR% (MTF 스코어, 로그용으로만 유지)
        atr_coin     = atr_from_ohlcv(ohlcv_1m[-15:], period=10)
        atr_pct_coin = atr_coin / curr_p if curr_p > 0 else HARD_SL_ATR_BASE
        atr_mult     = atr_pct_coin / HARD_SL_ATR_BASE if HARD_SL_ATR_BASE > 0 else 1.0

        # ★ V10.29d: ATR 기반 RSI shift 제거 — shift = 0 고정
        shift = 0

        adj_rsi5_os = max(20, rsi5_os - shift)
        adj_rsi5_ob = min(80, rsi5_ob + shift)

        # ★ V10.29d: RSI shift 전면 제거 — 35/65 완전 고정
        # (ATR 기반, urgency/skew 기반 모두 제거)

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
        # ★ V10.27d: EMA30 A/B 테스트
        ema_30_15m  = calc_ema(closes_15m, period=30) if len(closes_15m) >= 30 else None

        closes_5m_all = [float(x[4]) for x in ohlcv_5m]
        ema_20_5m     = calc_ema(closes_5m_all, period=20) if len(closes_5m_all) >= 20 else 0.0
        ema_30_5m     = calc_ema(closes_5m_all, period=30) if len(closes_5m_all) >= 30 else None

        # MR ATR 배수: 추세 강도에 따라 스무스 조절
        # EMA 괴리율로 추세 강도 측정
        _ema_gap = abs(ema_20_5m - ema_20_15m) / ema_20_15m if ema_20_15m > 0 else 0.0
        # ★ v10.14: ATR 부스트는 루프 밖에서 사전 계산됨 → 바로 사용

        # ★ v10.7: Skew 완화는 plan_soft_hedge에서 미러링으로 처리 (진입조건 왜곡 제거)

        # ★ v10.12: 롱 EMA10, 숏 EMA5
        mr_long_ok  = can_long  and ema_10_15m > 0 and (curr_p < ema_10_15m - atr_coin * _mr_atr_mult)
        # ★ V10.29c FIX: 기존 버그 — _mr_atr_mult_long 사용 → _mr_atr_mult 통일
        mr_short_ok = can_short and ema_10_15m > 0 and (curr_p > ema_10_15m + atr_coin * _mr_atr_mult)

        # ★ V10.29c: TREND용 raw 시그널 — 슬롯 체크 없이 ATR 이격만 판정
        _mr_signal_long  = ema_10_15m > 0 and (curr_p < ema_10_15m - atr_coin * _mr_atr_mult)
        _mr_signal_short = ema_10_15m > 0 and (curr_p > ema_10_15m + atr_coin * _mr_atr_mult)

        # ★ V10.29b: E30 전면 제거 — 7건 중 6건 손실, 건당 -$10
        mr_e30_long_ok  = False
        mr_e30_short_ok = False

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

        # ★ V10.29d: VS 거래량 — 로그용만 (필터 제거, 분석용 기록)
        _mr_vs_ok = True  # 필터 OFF
        _mr_vol_surge = 0.0
        _cur_regime = _btc_vol_regime(snapshot)
        _vol_15m = [float(x[5]) for x in ohlcv_15m] if ohlcv_15m else []
        if len(_vol_15m) >= 30:
            _vf = sum(_vol_15m[-5:]) / 5
            _vs = sum(_vol_15m[-30:]) / 30
            _mr_vol_surge = _vf / _vs if _vs > 0 else 1.0

        # ★ V10.29d: MTF RSI 필터 비활성화 — MTF 없이 19전 19승, 과도한 제한
        _mr_mtf_ok = True
        _mtf_rsi = 50.0

        # ★ V10.27d: MR 최종 — VS AND MTF 둘 다 통과해야 진입
        _mr_long_final  = mr_long_ok  and long_trig  and micro_long_ok  and _mr_vs_ok and _mr_mtf_ok
        _mr_short_final = mr_short_ok and short_trig and micro_short_ok and _mr_vs_ok and _mr_mtf_ok

        # ★ V10.29c: TREND용 시그널 — MR 품질 필터 통과했지만 슬롯 블록일 수 있음
        _trend_signal_long  = _mr_signal_long  and long_trig  and micro_long_ok  and _mr_vs_ok and _mr_mtf_ok
        _trend_signal_short = _mr_signal_short and short_trig and micro_short_ok and _mr_vs_ok and _mr_mtf_ok

        # ★ V10.29e: TREND 시그널 방향 — MR 슬롯 블록이어도 사용
        from v9.config import TREND_ENABLED, TREND_MIN_SCORE, TREND_COOLDOWN_SEC, TREND_MAX_SCORE
        _trend_signal_side = None
        if TREND_ENABLED:
            if _trend_signal_long:
                _trend_signal_side = "buy"
            elif _trend_signal_short:
                _trend_signal_side = "sell"

        # E30: EMA10 미충족 + EMA30 충족 + 같은 RSI/micro 조건 + 슬롯 여유
        _e30_long_final  = mr_e30_long_ok  and long_trig  and micro_long_ok  and _active_e30 < MAX_E30_SLOTS
        _e30_short_final = mr_e30_short_ok and short_trig and micro_short_ok and _active_e30 < MAX_E30_SLOTS

        # MR 우선, E30 보조
        _is_e30_entry = False
        if _mr_long_final:
            final_long_trig = True
        elif _e30_long_final:
            final_long_trig = True
            _is_e30_entry = True
        else:
            final_long_trig = False

        if _mr_short_final:
            final_short_trig = True
        elif _e30_short_final:
            final_short_trig = True
            _is_e30_entry = True
        else:
            final_short_trig = False

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
                # ★ V10.27d: E30 pending 발화 시 슬롯 한도 재확인
                if pend.get("entry_type") == "15mE30" and _active_e30 >= MAX_E30_SLOTS:
                    pending_map.pop(symbol, None)
                    continue
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
                        "entry_type":  "15mE30" if _is_e30_entry else "MR",
                        "reason":      f"HF_{'E30' if _is_e30_entry else 'MR'}_5mRSI({rsi5_now:.0f}/{adj_rsi5_ob})_ATR({atr_mult:.1f}x)_R({_cur_regime[0]})_VS({_mr_vol_surge:.1f})_MTF({_mtf_rsi:.0f})",
                    }
                    continue
                else:
                    trigger_side = "sell"
                    _e30_tag = "MR"
                    reason = f"HF_{_e30_tag}_5mRSI({rsi5_now:.0f}/{adj_rsi5_ob})_ATR({atr_mult:.1f}x)_R({_cur_regime[0]})_VS({_mr_vol_surge:.1f})_MTF({_mtf_rsi:.0f})"

            if final_long_trig and trigger_side is None:
                if OPEN_WAIT_NEXT_BAR:
                    pending_map[symbol] = {
                        "armed":       True,
                        "armed_ts_ms": last_5m_ts,
                        "side":        "buy",
                        "entry_type":  _pend_entry_type or ("15mE30" if _is_e30_entry else "MR"),
                        "reason":      f"HF_{'E30' if _is_e30_entry else 'MR'}_5mRSI({rsi5_now:.0f}/{adj_rsi5_os})_ATR({atr_mult:.1f}x)_R({_cur_regime[0]})_VS({_mr_vol_surge:.1f})_MTF({_mtf_rsi:.0f})",
                    }
                    continue
                else:
                    trigger_side = "buy"
                    _e30_tag = "MR"
                    reason = f"HF_{_e30_tag}_5mRSI({rsi5_now:.0f}/{adj_rsi5_os})_ATR({atr_mult:.1f}x)_R({_cur_regime[0]})_VS({_mr_vol_surge:.1f})_MTF({_mtf_rsi:.0f})"

        if trigger_side is None:
            # ★ V10.29e: MR 슬롯 블록이어도 TREND 시그널 → 최고 score 1개만 진입
            if _trend_signal_side:
                _tr_opp_side = "sell" if _trend_signal_side == "buy" else "buy"
                # ★ 방향 쿨다운 체크 (이전 NOSLOT/MR 연타 방지)
                if now_ts < _open_dir_cd.get(_tr_opp_side, 0.0):
                    continue
                _tr_opp_slots = _core_short if _tr_opp_side == "sell" else _core_long
                if _tr_opp_slots < MAX_MR_PER_SIDE:
                    _tr_best_sym = None
                    _tr_best_score = 0
                    _tr_ohlcv_pool = snapshot.ohlcv_pool or {}
                    _tr_prices = snapshot.all_prices or {}
                    _tr_held = {s for s, ss in st.items() if isinstance(ss, dict)
                                for _, p in iter_positions(ss)
                                if isinstance(p, dict) and float(p.get("amt", 0) or 0) > 0}
                    _tr_entered = {i.symbol for i in intents}
                    for _tr_sym in _tr_ohlcv_pool:
                        if _tr_sym == symbol or _tr_sym in _tr_held or _tr_sym in _tr_entered:
                            continue
                        if _tr_sym == "BTC/USDT":
                            continue
                        if _trend_cooldown.get(_tr_sym, 0) > now_ts:
                            continue
                        _tr_corr = (getattr(snapshot, "correlations", None) or {}).get(_tr_sym, 0)
                        if _tr_corr < OPEN_CORR_MIN:
                            continue
                        _tr_pool = _tr_ohlcv_pool.get(_tr_sym, {})
                        _tr_15m = _tr_pool.get("15m", [])
                        if len(_tr_15m) < 35:
                            continue
                        _tr_cp = float(_tr_prices.get(_tr_sym, 0))
                        if _tr_cp <= 0:
                            continue
                        _tr_score = _calc_trend_score(_tr_15m)
                        _TR_MIN = 0.5
                        # ★ V10.30: score 상한 (과열 역전 방지)
                        if abs(_tr_score) > TREND_MAX_SCORE:
                            continue
                        if _tr_opp_side == "sell" and _tr_score < -_TR_MIN:
                            if abs(_tr_score) > _tr_best_score:
                                _tr_best_score = abs(_tr_score)
                                _tr_best_sym = _tr_sym
                        elif _tr_opp_side == "buy" and _tr_score > _TR_MIN:
                            if _tr_score > _tr_best_score:
                                _tr_best_score = _tr_score
                                _tr_best_sym = _tr_sym
                    # ★ 글로벌 최고 score 비교 — 이전 심볼의 후보보다 높을 때만 갱신
                    if _tr_best_sym and (_noslot_best is None or _tr_best_score > _noslot_best["score"]):
                        _noslot_best = {
                            "sym": _tr_best_sym, "side": _tr_opp_side,
                            "score": _tr_best_score, "sig_sym": symbol,
                            "sig_side": _trend_signal_side,
                        }
                else:
                    print(f"[TREND_SKIP] {symbol} (MR블록) → COMP {_tr_opp_side} 슬롯풀({_tr_opp_slots}/{MAX_MR_PER_SIDE})")
            # ★ V10.30 FIX: trigger_side=None → MR 진입 코드 도달 차단
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
        # ── entry_type 확정 — V10.27d: MR + E30 A/B 테스트 ──
        if _pend_entry_type is not None:
            entry_type_tag = _pend_entry_type
        elif _is_e30_entry:
            entry_type_tag = "15mE30"
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
        # ★ V10.27d: E30 슬롯 카운터 증가 (루프 내 중복 방지)
        if entry_type_tag == "15mE30":
            _active_e30 += 1

        # ★ V10.29e: TREND COMPANION — MR 진입 성공 시 추세 심볼 동시 진입
        # (_trend_signal_side는 루프 상단에서 이미 계산됨)

        if _trend_signal_side:
            _tr_opp_side = "sell" if _trend_signal_side == "buy" else "buy"
            _tr_opp_slots = _core_short if _tr_opp_side == "sell" else _core_long
            _tr_entered = {i.symbol for i in intents}

            if _tr_opp_slots >= MAX_MR_PER_SIDE:
                print(f"[TREND_SKIP] {symbol} {trigger_side} → COMP {_tr_opp_side} 슬롯풀({_tr_opp_slots}/{MAX_MR_PER_SIDE})")
            else:
                _tr_best_sym = None
                _tr_best_score = 0
                _tr_ohlcv_pool = snapshot.ohlcv_pool or {}
                _tr_prices = snapshot.all_prices or {}
                _tr_held = {s for s, ss in st.items() if isinstance(ss, dict)
                            for _, p in iter_positions(ss)
                            if isinstance(p, dict) and float(p.get("amt", 0) or 0) > 0}

                for _tr_sym in _tr_ohlcv_pool:
                    if _tr_sym == symbol or _tr_sym in _tr_held or _tr_sym in _tr_entered:
                        continue
                    if _tr_sym == "BTC/USDT":
                        continue
                    if _trend_cooldown.get(_tr_sym, 0) > now_ts:
                        continue
                    # ★ V10.29d: corr 선체크 (risk_manager REJECT_CORR_LOW 방지)
                    _tr_corr = (getattr(snapshot, "correlations", None) or {}).get(_tr_sym, 0)
                    if _tr_corr < OPEN_CORR_MIN:
                        continue
                    _tr_pool = _tr_ohlcv_pool.get(_tr_sym, {})
                    _tr_15m = _tr_pool.get("15m", [])
                    if len(_tr_15m) < 35:
                        continue
                    _tr_cp = float(_tr_prices.get(_tr_sym, 0))
                    if _tr_cp <= 0:
                        continue

                    _tr_score = _calc_trend_score(_tr_15m)

                    # ★ V10.29d: 브레이크아웃 companion — 추세 방향 진입
                    # sell → 하락 추세 심볼 (score < -0.5)
                    # buy  → 상승 추세 심볼 (score > +0.5)
                    _TR_MIN = 0.5
                    # ★ V10.30: score 상한 (과열 역전 방지)
                    if abs(_tr_score) > TREND_MAX_SCORE:
                        continue
                    if _tr_opp_side == "sell" and _tr_score < -_TR_MIN:
                        if abs(_tr_score) > _tr_best_score:
                            _tr_best_score = abs(_tr_score)
                            _tr_best_sym = _tr_sym
                    elif _tr_opp_side == "buy" and _tr_score > _TR_MIN:
                        if _tr_score > _tr_best_score:
                            _tr_best_score = _tr_score
                            _tr_best_sym = _tr_sym

                if _tr_best_sym:
                    # ★ V10.31b: score 1.0~2.0 필터 — 애매한 트렌드 스킵
                    if 1.0 <= _tr_best_score < 2.0:
                        _skip_cd_key2 = f"_trend_skip_log_{_tr_best_sym}"
                        if time.time() - getattr(plan_open, _skip_cd_key2, 0) > 300:
                            setattr(plan_open, _skip_cd_key2, time.time())
                            print(f"[TREND_SCORE_SKIP] COMP {_tr_best_sym} score={_tr_best_score:.1f} "
                                  f"(1.0~2.0 필터) ← {symbol}")
                            try:
                                from v9.logging.logger_csv import log_system
                                log_system("TREND_SCORE_SKIP", f"COMP {_tr_best_sym} score={_tr_best_score:.1f} sig={symbol}")
                            except Exception: pass
                    else:
                        _tr_cp = float(_tr_prices.get(_tr_best_sym, 0))
                        _tr_grid = total_cap / GRID_DIVISOR * LEVERAGE
                        _tr_notional = _tr_grid * (DCA_WEIGHTS[0] / sum(DCA_WEIGHTS))
                        _tr_qty = _tr_notional / _tr_cp if _tr_cp > 0 and _tr_notional >= 10 else 0

                        if _tr_qty > 0:
                            _tr_dca_targets = _build_dca_targets(
                                _tr_cp, _tr_opp_side, _tr_grid, regime=_btc_regime)
                            _trend_cooldown[_tr_best_sym] = now_ts + TREND_COOLDOWN_SEC

                            # ★ V10.29d: MR fill 확인 후 발사 — system_state에 저장
                            # runner._process_pending_fill(OPEN)에서 꺼내서 실행
                            system_state["_pending_trend_comp"] = {
                                "symbol": _tr_best_sym,
                                "side": _tr_opp_side,
                                "qty": _tr_qty,
                                "price": _tr_cp,
                                "score": _tr_best_score,
                                "mr_symbol": symbol,
                                "dca_targets": _tr_dca_targets,
                                "regime": _btc_regime,
                                "ts": time.time(),
                            }
                            # ★ V10.29e: 동일심볼 헷지 시뮬레이션 — TREND_COMP와 비교용
                            _hsim = system_state.setdefault("_hedge_sim", {})
                            _hsim_opp = "buy" if trigger_side == "sell" else "sell"
                            _hsim[f"{symbol}:{trigger_side}"] = {
                                "ep": curr_p, "side": _hsim_opp,
                                "ts": time.time(), "mr_side": trigger_side,
                                "trend_sym": _tr_best_sym, "trend_side": _tr_opp_side,
                                "trend_ep": _tr_cp,
                            }
                            print(f"[HEDGE_SIM] 📊 {symbol} {_hsim_opp} ep={curr_p:.4f} "
                                  f"(vs TREND {_tr_best_sym} {_tr_opp_side} ep={_tr_cp:.4f})")
                            try:
                                log_system("HEDGE_SIM", f"{symbol} {_hsim_opp} ep={curr_p:.4f} vs {_tr_best_sym} {_tr_opp_side}")
                            except Exception: pass
                            print(f"[TREND] {_tr_best_sym} {_tr_opp_side} score={_tr_best_score:.1f} "
                                  f"← sig {symbol} {_trend_signal_side} (PENDING→MR fill)")
                        try:
                            from v9.logging.logger_csv import log_system
                            log_system("TREND", f"{_tr_best_sym} {_tr_opp_side} score={_tr_best_score:.1f} ← {symbol} PENDING")
                        except Exception: pass
                else:
                    print(f"[TREND_SKIP] {symbol} {trigger_side} → COMP {_tr_opp_side} 후보없음(score미달/보유중/corr)")
        elif TREND_ENABLED:
            # ★ V10.29e: TREND 시그널 미감지 사유 로그
            _ts_reasons = []
            if not _trend_signal_long and not _trend_signal_short:
                if not _mr_vs_ok: _ts_reasons.append("VS")
                if not _mr_mtf_ok: _ts_reasons.append("MTF")
                if not long_trig and not short_trig: _ts_reasons.append("ATR")
                if not micro_long_ok and not micro_short_ok: _ts_reasons.append("MICRO")
            print(f"[TREND_SKIP] {symbol} {trigger_side} → 시그널없음({','.join(_ts_reasons) or 'N/A'})")

    # ★ V10.29e: TREND_NOSLOT — 루프 종료 후 최고 score 1개만 발사
    if _noslot_best:
        _ns = _noslot_best
        # ★ V10.31b: score 1.0~2.0 필터
        if 1.0 <= _ns["score"] < 2.0:
            # ★ V10.31b: 로그 스팸 방지 (심볼당 5분 1회)
            _skip_cd_key = f"_trend_skip_log_{_ns['sym']}"
            if time.time() - getattr(plan_open, _skip_cd_key, 0) > 300:
                setattr(plan_open, _skip_cd_key, time.time())
                print(f"[TREND_SCORE_SKIP] NOSLOT {_ns['sym']} score={_ns['score']:.1f} "
                      f"(1.0~2.0 필터) ← {_ns['sig_sym']}")
                try:
                    from v9.logging.logger_csv import log_system
                    log_system("TREND_SCORE_SKIP", f"NOSLOT {_ns['sym']} score={_ns['score']:.1f} sig={_ns['sig_sym']}")
                except Exception: pass
            _noslot_best = None
    if _noslot_best:
        _ns = _noslot_best
        _ns_prices = snapshot.all_prices or {}
        _ns_cp = float(_ns_prices.get(_ns["sym"], 0))
        if _ns_cp > 0:
            _ns_total_cap = _mr_available_balance(snapshot, st)  # ★ V10.31b: BC 차감
            _ns_grid = _ns_total_cap / GRID_DIVISOR * LEVERAGE
            _ns_notional = _ns_grid * (DCA_WEIGHTS[0] / sum(DCA_WEIGHTS))
            _ns_qty = _ns_notional / _ns_cp if _ns_notional >= 10 else 0
            if _ns_qty > 0:
                _ns_dca = _build_dca_targets(_ns_cp, _ns["side"], _ns_grid, regime=_btc_regime)
                _trend_cooldown[_ns["sym"]] = time.time() + TREND_COOLDOWN_SEC
                intents.append(Intent(
                    trace_id=_tid(),
                    intent_type=IntentType.OPEN,
                    symbol=_ns["sym"],
                    side=_ns["side"],
                    qty=_ns_qty,
                    price=None,
                    reason=f"TREND_NOSLOT(sig={_ns['sig_sym']},score={_ns['score']:.1f})",
                    metadata={
                        "atr": 0.0, "dca_targets": _ns_dca,
                        "role": "CORE_MR", "entry_type": "TREND",
                        "positionSide": "LONG" if _ns["side"] == "buy" else "SHORT",
                        "locked_regime": _btc_regime,
                    },
                ))
                _ns_corr = (getattr(snapshot, "correlations", None) or {}).get(_ns["sym"], 0)
                print(f"[TREND_NOSLOT] ⚡ {_ns['sym']} {_ns['side']} score={_ns['score']:.1f} "
                      f"corr={_ns_corr:.2f} ← {_ns['sig_sym']} (최고score 발사)")
                # ★ V10.29e: MR과 동일 방향 쿨다운 — 같은 시그널 연타 방지
                _open_dir_cd[_ns["side"]] = time.time() + OPEN_DIR_COOLDOWN_SEC
                try:
                    from v9.logging.logger_csv import log_system
                    log_system("TREND_NOSLOT", f"{_ns['sym']} {_ns['side']} score={_ns['score']:.1f} corr={_ns_corr:.2f} FIRE")
                except Exception: pass

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

    # ★ V10.29c: DCA 제한 데드코드 정리 — killswitch만 유지
    _killswitch_dca = float(getattr(snapshot, "margin_ratio", 0.0) or 0.0) >= 0.8

    # _hard_sl_history TTL 정리 (24시간 초과 제거, force_close에서 append됨)
    _hsl_raw = (system_state or {}).get("_hard_sl_history", [])
    if _hsl_raw and len(_hsl_raw) > 50:
        _cutoff = now - 86400
        system_state["_hard_sl_history"] = [e for e in _hsl_raw if isinstance(e, dict) and e.get("ts", 0) > _cutoff]

    # ★ V10.29e: 스큐 기반 DCA 로직 전면 제거 — TREND 진입이 스큐 해소
    _dca_positions = list(_pos_items(st))

    for symbol, p in _dca_positions:

        # ★ V10.29d: BC/CB는 자체 관리 — MR DCA 제외
        if p.get("role") in ("BC", "CB"):
            continue
        # ★ V10.29e: DCA 선주문 활성이면 plan_dca 스킵 (중복 방지)
        # DCA는 기존 포지션 평단 개선이므로 슬롯 균형과 무관하게 허용
        # 스큐 완화는 진입 차단 + TP 할인으로 충분

        # ★ V10.28: heavy side DCA 차단 제거 — MR 핵심 기능(평단 압축) 보존
        # 차단 시 T1 고립 → HARD_SL 확률↑ → 작은 돈으로 자주 짐
        # DCA 허용 + 독립 Trim(tier별 +2% 익절)이 중간 손절 역할
        # 스큐 완화는 진입 ATR 패널티 + TP 할인 + light block으로 충분
        # (기존 urgency ≥10/15/20 단계별 heavy DCA 차단 삭제)

        # ★ V10.27: ATR LOW DCA 게이트 제거 (MR 전략은 횡보장에서 유리, DCA 제한 역효과)

        # ★ V10.22: DCA 하드가드 (4단)
        # 1) dca_level >= 3 이면 스킵 (3단 DCA 구조)
        if int(p.get("dca_level", 1) or 1) >= 3:
            continue
        # 2) max_dca_reached 플래그 (dca_level 리셋 버그 방어)
        if p.get("max_dca_reached"):
            continue
        # ★ V10.30: DCA 선주문 LIMIT 활성이면 스킵, stale이면 정리
        _dca_pre = p.get("dca_preorders", {})
        if _dca_pre:
            _any_live = False
            try:
                from v9.execution.order_router import _PENDING_LIMITS
                for _dt, _di in list(_dca_pre.items()):
                    if _di.get("oid") and _di["oid"] in _PENDING_LIMITS:
                        _any_live = True
                    else:
                        _dca_pre.pop(_dt, None)  # stale 정리
            except Exception:
                _dca_pre.clear()
            if _any_live:
                continue  # LIMIT 대기 중 → plan_dca 스킵
        # ★ V10.29b: 노셔널 95% 캡 제거 — DCA 무조건 허용

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

        # ★ V10.28b: Entry 기준 DCA — 이전 tier entry에서 -1.8% ROI
        _ref_ep = float(p.get("ep", 0.0) or 0.0)
        roi_now = calc_roi_pct(_ref_ep, curr_p, p.get("side", ""), LEVERAGE) if _ref_ep > 0 else 0.0

        for target in dca_targets:
            tier_now = target.get("tier", 2)

            # ★ V10.28b FIX: 순차 DCA만 허용 — 반드시 다음 tier만 (T1→T2→T3→T4)
            _curr_dca_level = int(p.get("dca_level", 1) or 1)
            if tier_now != _curr_dca_level + 1:
                continue

            # ★ V10.28b: 이전 tier entry 기준 ROI로 트리거
            if DCA_ENTRY_BASED:
                _prev_tier_ep_map = {
                    2: float(p.get("original_ep", p.get("ep", 0)) or 0),
                    3: float(p.get("t2_entry_price", 0) or 0),
                    4: float(p.get("t3_entry_price", 0) or 0),
                }
                _prev_ep = _prev_tier_ep_map.get(tier_now, 0)
                # ★ V10.28b FIX: 이전 tier entry 없으면 스킵 (blended EP fallback 제거)
                if _prev_ep <= 0:
                    continue
                _entry_roi = calc_roi_pct(_prev_ep, curr_p, p.get("side", ""), LEVERAGE)
                # ★ V10.29: 티어별 DCA 거리 (T3/T4 두배)
                _dca_roi_thresh = DCA_ENTRY_ROI_BY_TIER.get(tier_now, DCA_ENTRY_ROI)
                is_hit = _entry_roi <= _dca_roi_thresh
            else:
                roi_trig = DCA_ROI_TRIGGERS.get(tier_now, -8.0)
                is_hit = roi_now <= roi_trig
            if not is_hit:
                continue

            # ★ PATCH BUG3: 루프 내부에서 tier_now 기준 쿨다운 계산
            tier_cooldown = DCA_COOLDOWN_BY_TIER.get(tier_now, DCA_COOLDOWN_SEC)

            # ★ V10.29b: DCA 차단 로직 전면 제거 — 스윙 T3 진입 보장
            # killswitch만 유지 (수동 비상 정지)
            _block = None
            if _killswitch_dca:
                _block = "KILLSWITCH"
            if _block:
                print(f"[DCA_BLOCKED] {symbol} DCA T{tier_now} ROI hit "
                      f"(ep={_ref_ep:.4f} roi={roi_now:.2f}%) "
                      f"but blocked ({_block})")
                break  # 차단 → 다음 심볼

            # ── 차단 아님 → DCA 진입 ──
            # ★ V10.30: 노셔널 기반 DCA sizing — 목표 대비 부족분만 주문
            from v9.config import calc_tier_notional
            _dca_bal = float(getattr(snapshot, "real_balance_usdt", 0) or 0)
            _target_notional = calc_tier_notional(tier_now, _dca_bal)
            _current_notional = float(p.get("amt", 0) or 0) * curr_p
            _dca_notional = _target_notional - _current_notional
            if _dca_notional <= 0:
                print(f"[DCA_GUARD] {symbol} T{tier_now} 이미 목표 도달 "
                      f"(보유${_current_notional:.0f} ≥ 목표${_target_notional:.0f}) → skip")
                continue
            qty = _dca_notional / curr_p if curr_p > 0 else 0.0
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
    """★ V10.31b: T1 TP1 — trail 방식 통합.

    tp1_preorder_id / tp1_limit_oid 가드 제거.
    T1: ROI ≥ TP1_FIXED → trail 활성화 → max-gap 하회 시 partial close → step=1 → TRAIL_ON.
    T2+: plan_trim_trail()이 독립 처리.
    """
    from v9.config import (TRIM_TRAIL_FLOOR, HARD_SL_ATR_BASE,
                           calc_tier_notional, notional_to_qty)
    intents: List[Intent] = []
    _tp1_excl = exclude_syms or set()

    for symbol, p in _pos_items(st):
        if symbol in _tp1_excl:
            continue
        # ★ V10.31b: tp1_limit_oid는 사용 안 함 → 항상 정리
        _stale_lim = p.pop("tp1_limit_oid", None)
        if _stale_lim:
            from v9.strategy.strategy_core import _TRIM_CANCEL_QUEUE
            _TRIM_CANCEL_QUEUE.append({"sym": symbol, "oid": _stale_lim})
        if p.get("step", 0) != 0 or p.get("tp1_done"):
            continue
        if p.get("pending_close"):
            continue
        _role = p.get("role", "")
        if _role in ("BC", "CB"):
            continue
        if _role in ("INSURANCE_SH", "CORE_HEDGE"):
            continue
        # HEDGE 계열 → 전용 모듈 위임
        if _role in ("HEDGE", "SOFT_HEDGE"):
            curr_p = float((snapshot.all_prices or {}).get(symbol, 0.0))
            if curr_p <= 0:
                continue
            from v9.engines.hedge_engine_v2 import check_hedge_tp1
            _tp1_ok, roi_gross, tp1_thresh = check_hedge_tp1(p, curr_p)
            if not _tp1_ok:
                continue
            p["step"] = 1
            p["tp1_done"] = True
            p["trailing_on_time"] = time.time()
            print(f"[SOFT_HEDGE] {symbol} TP1 {roi_gross:.1f}% → 100% trailing")
            continue

        dca_level = int(p.get("dca_level", 1) or 1)
        # T2+는 plan_trim_trail()이 처리
        if dca_level >= 2:
            continue

        # ★ V10.31b: 레짐별 exit 분기
        _regime = _btc_vol_regime(snapshot)
        if _regime != "HIGH":
            # LOW/NORMAL: _manage_tp1_preorders가 처리 → trail 정리 + skip
            if p.get("trim_trail_active"):
                p["trim_trail_active"] = False
                p["trim_trail_max"] = 0.0
            continue

        # ── HIGH: trail 모드 ──
        # 이전 LOW/NORMAL에서 남은 선주문 정리
        _stale_pre = p.pop("tp1_preorder_id", None)
        if _stale_pre and _stale_pre != "DRY_PREORDER":
            from v9.strategy.strategy_core import _TRIM_CANCEL_QUEUE
            _TRIM_CANCEL_QUEUE.append({"sym": symbol, "oid": _stale_pre})
            p.pop("tp1_preorder_price", None)
            p.pop("tp1_preorder_ts", None)
            print(f"[TP_TRAIL] {symbol} LOW→HIGH 전환: 선주문 취소 {_stale_pre}")

        curr_p = float((snapshot.all_prices or {}).get(symbol, 0.0))
        if curr_p <= 0:
            continue
        is_long = p.get("side", "") == "buy"
        roi_gross = calc_roi_pct(p.get("ep", 0.0), curr_p, p.get("side", ""), LEVERAGE)

        # ★ T1 trail 임계치
        _worst = float(p.get("worst_roi", 0.0) or 0.0)
        tp1_thresh = calc_tp1_thresh(dca_level, _worst)

        # ── trail 활성화 ──
        if roi_gross >= tp1_thresh and not p.get("trim_trail_active"):
            p["trim_trail_active"] = True
            p["trim_trail_max"] = roi_gross
            print(f"[TP_TRAIL] {symbol} T{dca_level} 활성화 "
                  f"roi={roi_gross:.2f}%≥{tp1_thresh}")

        if not p.get("trim_trail_active"):
            continue

        # ── max 갱신 ──
        _max = float(p.get("trim_trail_max", 0) or 0)
        if roi_gross > _max:
            p["trim_trail_max"] = roi_gross
            _max = roi_gross

        # ── ATR 기반 gap ──
        _pool = (snapshot.ohlcv_pool or {}).get(symbol, {})
        _15m = _pool.get("15m", [])
        _atr = atr_from_ohlcv(_15m[-15:], period=10) if len(_15m) >= 15 else 0.0
        _atr_pct = (_atr / curr_p) if (curr_p > 0 and _atr > 0) else HARD_SL_ATR_BASE * 3
        if _atr_pct < HARD_SL_ATR_BASE * 2:
            _gap = 0.2
        elif _atr_pct < HARD_SL_ATR_BASE * 5:
            _gap = 0.3
        else:
            _gap = 0.5

        # ── 발동 체크 ──
        _stop = _max - _gap
        _fire = False
        _reason = ""
        if roi_gross <= _stop:
            _fire = True
            _reason = f"TP_TRAIL(max={_max:.1f},gap={_gap:.2f},roi={roi_gross:.1f})"
        elif roi_gross <= TRIM_TRAIL_FLOOR:
            _fire = True
            _reason = f"TP_FLOOR(roi={roi_gross:.1f}≤{TRIM_TRAIL_FLOOR})"

        if not _fire:
            continue

        # ── T1 partial close (→ step=1 → TRAIL_ON) ──
        p["trim_trail_active"] = False
        p["trim_trail_max"] = 0.0

        total_qty = float(p.get("amt", 0.0))
        _tp_bal = float(getattr(snapshot, 'real_balance_usdt', 0) or 0)
        _t1_notional = calc_tier_notional(1, _tp_bal) if _tp_bal > 0 else 0
        if _t1_notional > 0 and curr_p > 0:
            _t1_qty = notional_to_qty(_t1_notional, curr_p)
            close_qty = _t1_qty * TP1_PARTIAL_RATIO
        else:
            close_qty = total_qty * TP1_PARTIAL_RATIO
        _sym_min_qty = SYM_MIN_QTY.get(symbol, SYM_MIN_QTY_DEFAULT)
        if close_qty < _sym_min_qty:
            close_qty = total_qty
        _remaining = total_qty - close_qty
        if 0 < _remaining < _sym_min_qty:
            close_qty = total_qty
        close_qty = min(close_qty, total_qty)
        if close_qty <= 0:
            continue
        intents.append(Intent(
            trace_id=_tid(),
            intent_type=IntentType.TP1,
            symbol=symbol,
            side="sell" if is_long else "buy",
            qty=close_qty,
            price=curr_p,
            reason=f"TP1_{_reason}_T{dca_level}",
            metadata={"roi_gross": roi_gross, "tp1_thresh": tp1_thresh,
                      "force_market": True},
        ))
    return intents


# ═════════════════════════════════════════════════════════════════
# TRIM TRAIL Planner (V10.31 — T2+ 독립 분리)
# ═════════════════════════════════════════════════════════════════
def plan_trim_trail(snapshot: MarketSnapshot, st: Dict,
                    exclude_syms: set = None) -> List[Intent]:
    """★ V10.31: T2+ Trim Trail — plan_tp1에서 완전 분리.

    guard 최소화: position이 존재하고 dca_level >= 2이면 무조건 trail 체크.
    tp1_preorder_id, tp1_limit_oid, step, tp1_done 등 T1 전용 필드 무시.
    """
    from v9.config import (TRIM_BLENDED_ROI_BY_TIER, TRIM_TRAIL_FLOOR,
                           HARD_SL_ATR_BASE, calc_trim_qty)
    intents: List[Intent] = []
    _excl = exclude_syms or set()

    for symbol, p in _pos_items(st):
        if symbol in _excl:
            continue
        # ── 최소 guard: 수량 있고, T2+이고, BC/CB 아닌 것만 ──
        amt = float(p.get("amt", 0) or 0)
        if amt <= 0:
            continue
        dca_level = int(p.get("dca_level", 1) or 1)
        if dca_level < 2:
            continue
        if p.get("role") in ("BC", "CB"):
            continue
        if p.get("pending_close"):
            continue

        # ★ V10.31b: 레짐별 exit 분기
        _regime = _btc_vol_regime(snapshot)
        if _regime != "HIGH":
            # LOW/NORMAL: _place_trim_preorders가 처리 → trail 정리 + skip
            if p.get("trim_trail_active"):
                p["trim_trail_active"] = False
                p["trim_trail_max"] = 0.0
            continue

        # ── HIGH: trail 모드 ──
        # 이전 LOW/NORMAL에서 남은 trim 선주문 취소 (이중 exit 방지)
        _trp = p.get("trim_preorders", {})
        if _trp:
            for _ct, _ci in list(_trp.items()):
                if isinstance(_ci, dict) and _ci.get("oid"):
                    from v9.strategy.strategy_core import _TRIM_CANCEL_QUEUE
                    _TRIM_CANCEL_QUEUE.append({"sym": symbol, "oid": _ci["oid"]})
            p["trim_preorders"] = {}
            p.pop("trim_to_place", None)

        curr_p = float((snapshot.all_prices or {}).get(symbol, 0.0))
        if curr_p <= 0:
            continue

        is_long = p.get("side", "") == "buy"
        roi = calc_roi_pct(p.get("ep", 0.0), curr_p, p.get("side", ""), LEVERAGE)
        _threshold = TRIM_BLENDED_ROI_BY_TIER.get(dca_level, 1.0)

        # ── trail 활성화 ──
        if roi >= _threshold and not p.get("trim_trail_active"):
            p["trim_trail_active"] = True
            p["trim_trail_max"] = roi
            print(f"[TRIM_TRAIL] {symbol} T{dca_level} 활성화 "
                  f"roi={roi:.2f}%≥{_threshold}")

        if not p.get("trim_trail_active"):
            continue

        # ── max 갱신 ──
        _max = float(p.get("trim_trail_max", 0) or 0)
        if roi > _max:
            p["trim_trail_max"] = roi
            _max = roi

        # ── ATR 기반 gap ──
        _pool = (snapshot.ohlcv_pool or {}).get(symbol, {})
        _15m = _pool.get("15m", [])
        _atr = atr_from_ohlcv(_15m[-15:], period=10) if len(_15m) >= 15 else 0.0
        _atr_pct = (_atr / curr_p) if (curr_p > 0 and _atr > 0) else HARD_SL_ATR_BASE * 3
        if _atr_pct < HARD_SL_ATR_BASE * 2:
            _gap = 0.2
        elif _atr_pct < HARD_SL_ATR_BASE * 5:
            _gap = 0.3
        else:
            _gap = 0.5

        # ── 발동 체크 ──
        _stop = _max - _gap
        _fire = False
        _reason = ""

        if roi <= _stop:
            _fire = True
            _reason = f"TRIM_TRAIL(max={_max:.1f},gap={_gap:.2f},roi={roi:.1f})"
        elif roi <= TRIM_TRAIL_FLOOR:
            _fire = True
            _reason = f"TRIM_FLOOR(roi={roi:.1f}≤{TRIM_TRAIL_FLOOR})"

        if not _fire:
            continue

        # ── trim 실행 ──
        p["trim_trail_active"] = False
        p["trim_trail_max"] = 0.0
        _bal = float(getattr(snapshot, "real_balance_usdt", 0) or 0)
        _ep = float(p.get("ep", 0) or 0)
        trim_qty = calc_trim_qty(amt, dca_level, ep=_ep, bal=_bal, mark_price=curr_p)
        _min_qty = SYM_MIN_QTY.get(symbol, SYM_MIN_QTY_DEFAULT)
        if trim_qty < _min_qty:
            continue
        intents.append(Intent(
            trace_id=_tid(),
            intent_type=IntentType.TP1,
            symbol=symbol,
            side="sell" if is_long else "buy",
            qty=trim_qty,
            price=curr_p,
            reason=f"DCA_{_reason}→T{dca_level-1}_T{dca_level}",
            metadata={"roi_gross": roi, "is_trim": True,
                      "target_tier": dca_level - 1,
                      "force_market": True},
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
            # ★ V10.29d: BC/CB는 자체 trail — MR trail 제외
            if (p or {}).get("role") in ("BC", "CB"):
                continue
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

            roi_pct = calc_roi_pct(p.get("ep", 0.0), curr_p, p.get("side", ""), LEVERAGE)
            max_roi = p.get("max_roi_seen", roi_pct)
            if roi_pct > max_roi:
                p["max_roi_seen"] = roi_pct
                continue

            # ── MR/Pullback 통합 트레일링 (step >= 1 필요) ─────────────
            step = p.get("step", 0)
            if step < 1:
                continue

            # ── ATR (타임아웃 동적 보정용) ──
            pool     = (snapshot.ohlcv_pool or {}).get(symbol, {})
            ohlcv_1m = pool.get("1m", [])
            atr_live = atr_from_ohlcv(ohlcv_1m[-15:], period=10) if len(ohlcv_1m) >= 15 else 0.0
            atr_val  = atr_live if atr_live > 0 else p.get("atr", 0.0)
            atr_ratio = (
                ((atr_val / curr_p) / HARD_SL_ATR_BASE)
                if (atr_val > 0 and curr_p > 0) else 1.0
            )

            trailing_triggered = False
            trail_reason       = "TRAILING_STOP"

            # ★ V10.30: 15m ATR 구간별 trail gap 선택
            _t1_15m = pool.get("15m", [])
            _t1_atr = atr_from_ohlcv(_t1_15m[-15:], period=10) if len(_t1_15m) >= 15 else 0.0
            _t1_atr_pct = (_t1_atr / curr_p) if (curr_p > 0 and _t1_atr > 0) else HARD_SL_ATR_BASE * 3
            if _t1_atr_pct < HARD_SL_ATR_BASE * 2:      # 저변동
                _trail_gap = 0.2
            elif _t1_atr_pct < HARD_SL_ATR_BASE * 5:     # 정상
                _trail_gap = 0.3
            else:                                          # 고변동
                _trail_gap = 0.5
            _stop = max_roi - _trail_gap
            if roi_pct <= _stop:
                trailing_triggered = True
                trail_reason = f"FTRAIL_{_trail_gap:.2f}(max={max_roi:.1f},stop={_stop:.2f})"

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
                # ★ V10.29e FIX: trail close는 p["amt"] 전량 — 잔량 남으면 유령 포지션
                _trail_qty = float(p.get("amt", 0.0))
                intents.append(Intent(
                    trace_id=_tid(),
                    intent_type=IntentType.TRAIL_ON,
                    symbol=symbol,
                    side="sell" if is_long else "buy",
                    qty=_trail_qty,
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


# ★ V10.29e: plan_force_close → v9/engines/hedge_engine.py 분리 (데드파일 재활용)
# ═════════════════════════════════════════════════════════════════
# ★ V10.29e: Insurance SH 복원 — BTC 급변 시 피해측 반대 헷지
# ═════════════════════════════════════════════════════════════════
def plan_insurance_sh(
    snapshot: MarketSnapshot,
    st: Dict,
    system_state: Dict,
) -> List[Intent]:
    """BTC 급변 직접 감지 → 피해측 최약 포지션 50% 반대 헷지."""
    intents: List[Intent] = []
    now = time.time()

    _boot_ts = float(system_state.get("_boot_ts", 0.0) or 0.0)
    if _boot_ts > 0 and (now - _boot_ts) < 300:
        return intents

    from v9.config import INSURANCE_COOLDOWN_SEC, INSURANCE_SIZE_RATIO, INSURANCE_MIN_AFFECTED
    _last_ins = float(system_state.get("_insurance_last_ts", 0.0) or 0.0)
    if now - _last_ins < INSURANCE_COOLDOWN_SEC:
        return intents

    for _s, _ss in st.items():
        if not isinstance(_ss, dict): continue
        for _, _pp in iter_positions(_ss):
            if isinstance(_pp, dict) and _pp.get("role") == "INSURANCE_SH":
                return intents

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
        if len(btc_1m) < bars + 1: continue
        ref_p = float(btc_1m[-(bars + 1)][4])
        if ref_p <= 0: continue
        ret = (now_p - ref_p) / ref_p
        if ret <= -threshold:
            event_detected = True
            affected_side = "buy"
            event_mag = abs(ret)
            event_bars = bars
            break
        elif ret >= threshold:
            event_detected = True
            affected_side = "sell"
            event_mag = abs(ret)
            event_bars = bars
            break

    if not event_detected:
        return intents

    affected_positions = []
    for sym, sym_st in st.items():
        if not isinstance(sym_st, dict): continue
        p = get_p(sym_st, affected_side)
        if not isinstance(p, dict): continue
        if p.get("role", "") in ("INSURANCE_SH", "CORE_HEDGE", "HEDGE", "SOFT_HEDGE"): continue
        cp = float((snapshot.all_prices or {}).get(sym, 0.0))
        if cp <= 0: continue
        ep = float(p.get("ep", 0.0) or 0.0)
        if ep <= 0: continue
        roi = calc_roi_pct(ep, cp, affected_side, LEVERAGE)
        affected_positions.append((sym, p, cp, roi))

    if len(affected_positions) < INSURANCE_MIN_AFFECTED:
        return intents

    affected_positions.sort(key=lambda x: x[3])
    worst_sym, worst_p, worst_cp, worst_roi = affected_positions[0]

    if int(worst_p.get("step", 0) or 0) >= 1:
        return intents

    hedge_side = "sell" if affected_side == "buy" else "buy"
    opp = get_p(st.get(worst_sym, {}), hedge_side)
    if isinstance(opp, dict):
        return intents

    _mr_ins = float(getattr(snapshot, "margin_ratio", 0.0) or 0.0)
    if _mr_ins >= 0.90:
        return intents

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
            "atr": 0.0, "dca_targets": [],
            "role": "INSURANCE_SH", "entry_type": "INSURANCE_SH",
            "source_sym": worst_sym, "hedge_entry_price": now_p,
            "insurance_timecut": 0,
            "positionSide": "LONG" if hedge_side == "buy" else "SHORT",
            "locked_regime": "LOW",
        },
    ))
    system_state["_insurance_last_ts"] = now
    print(f"[INSURANCE_SH] BTC {event_bars}m {event_mag*100:.1f}% → "
          f"{worst_sym} {hedge_side} (src_roi={worst_roi:+.1f}%, "
          f"qty={qty:.4f}, btc={now_p:.0f})")
    return intents


from v9.engines.hedge_engine import plan_force_close, save_exit_state, restore_exit_state
# ═════════════════════════════════════════════════════════════════
# 전체 Intent 생성
# ═════════════════════════════════════════════════════════════════
# ★ V10.29e: plan_counter → v9/engines/dca_engine.py 분리 (기존 데드파일 재활용)
from v9.engines.dca_engine import plan_counter, save_counter_state, restore_counter_state


# ═════════════════════════════════════════════════════════════════
# PRE-MARKET CLEAR (V10.31b — 미장 전 포지션 정리)
# ═════════════════════════════════════════════════════════════════
def plan_pre_market_clear(snapshot: MarketSnapshot, st: Dict,
                          system_state: Dict) -> List[Intent]:
    """★ V10.31b: 미장 오픈 전 포지션 정리 (DST 자동 반영).

    08:00 ET: 신규 진입 차단 (system_state 플래그)
    08:30 ET: 전 포지션 시장가 정리
    """
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    intents: List[Intent] = []
    et = datetime.now(ZoneInfo("America/New_York"))
    et_min = et.hour * 60 + et.minute
    today_key = et.strftime("%Y-%m-%d")

    # ★ 주말(토/일) + NYSE 공휴일 → 스킵
    if et.weekday() >= 5:
        return intents
    _NYSE_HOLIDAYS_2026 = {
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
        "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
        "2026-11-26", "2026-12-25",
    }
    if today_key in _NYSE_HOLIDAYS_2026:
        return intents

    BLOCK_START = 480   # ET 08:00 — 신규 진입 차단 + limit 배치
    CLEAR_START = 510   # ET 08:30 — 잔존 시장가 정리 + 차단 해제
    CLEAR_END   = 570   # ET 09:30 — Phase 2 실행 허용 윈도우

    # ── 08:00~08:30: 신규 진입 차단 ──
    if BLOCK_START <= et_min < CLEAR_START:
        system_state["_pmc_block_entry"] = True
    else:
        system_state.pop("_pmc_block_entry", None)

    # ── Phase 1 (08:00): limit +0.5% 배치 (1회) ──
    if BLOCK_START <= et_min < CLEAR_START:
        p1_key = f"_pmc_p1_{today_key}"
        if system_state.get(p1_key):
            return intents

        for symbol, p in _pos_items(st):
            if p.get("role") in ("BC", "CB", "HEDGE", "SOFT_HEDGE",
                                 "INSURANCE_SH", "CORE_HEDGE"):
                continue
            amt = float(p.get("amt", 0) or 0)
            if amt <= 0:
                continue
            curr_p = float((snapshot.all_prices or {}).get(symbol, 0.0))
            if curr_p <= 0:
                continue

            is_long = p.get("side", "") == "buy"
            close_price = curr_p * 1.005 if is_long else curr_p * 0.995
            dca_level = int(p.get("dca_level", 1) or 1)

            # DCA 선주문 취소
            for _dt, _di in list(p.get("dca_preorders", {}).items()):
                if isinstance(_di, dict) and _di.get("oid"):
                    from v9.strategy.strategy_core import _TRIM_CANCEL_QUEUE
                    _TRIM_CANCEL_QUEUE.append({"sym": symbol, "oid": _di["oid"]})
            p["dca_preorders"] = {}

            intents.append(Intent(
                trace_id=_tid(),
                intent_type=IntentType.CLOSE,
                symbol=symbol,
                side="sell" if is_long else "buy",
                qty=amt,
                price=close_price,
                reason=f"PRE_MKT_P1_T{dca_level}(+0.5%)",
                metadata={"pre_market_limit": True,
                          "_expected_role": p.get("role", "")},
            ))

        if intents:
            system_state[p1_key] = True
            print(f"[PRE_MKT] Phase 1: {len(intents)}건 limit +0.5% 배치 "
                  f"(ET {et.strftime('%H:%M')})")
        return intents

    # ── Phase 2 (08:30): 잔존 포지션 시장가 정리 (1회) ──
    if CLEAR_START <= et_min < CLEAR_END:
        clear_key = f"_pmc_clear_{today_key}"
        if system_state.get(clear_key):
            return intents

        for symbol, p in _pos_items(st):
            if p.get("role") in ("BC", "CB", "HEDGE", "SOFT_HEDGE",
                                 "INSURANCE_SH", "CORE_HEDGE"):
                continue
            amt = float(p.get("amt", 0) or 0)
            if amt <= 0:
                continue
            curr_p = float((snapshot.all_prices or {}).get(symbol, 0.0))
            if curr_p <= 0:
                continue

            is_long = p.get("side", "") == "buy"
            dca_level = int(p.get("dca_level", 1) or 1)

            # DCA 선주문 취소
            for _dt, _di in list(p.get("dca_preorders", {}).items()):
                if isinstance(_di, dict) and _di.get("oid"):
                    from v9.strategy.strategy_core import _TRIM_CANCEL_QUEUE
                    _TRIM_CANCEL_QUEUE.append({"sym": symbol, "oid": _di["oid"]})
            p["dca_preorders"] = {}

            intents.append(Intent(
                trace_id=_tid(),
                intent_type=IntentType.FORCE_CLOSE,
                symbol=symbol,
                side="sell" if is_long else "buy",
                qty=amt,
                price=curr_p,
                reason=f"PRE_MKT_P2_T{dca_level}",
                metadata={"force_market": True,
                          "_expected_role": p.get("role", "")},
            ))

        if intents:
            system_state[clear_key] = True
            print(f"[PRE_MKT] Phase 2: {len(intents)}건 잔존 시장가 정리 "
                  f"(ET {et.strftime('%H:%M')})")
        return intents

    return intents


def generate_all_intents(
    snapshot: MarketSnapshot,
    st: Dict,
    cooldowns: Dict,
    system_state: Dict,
) -> List[Intent]:
    """
    ★ V10.29e 실행 순서:
      1. plan_force_close       → HARD_SL / ZOMBIE / TIMECUT
      2. plan_tp1               → TP1 Skew-Aware 부분익절
      3. plan_trail_on          → 트레일링 스탑
      4. plan_dca               → DCA 4단 진입
      5. plan_counter           → BB Squeeze 브레이크아웃
      6. plan_open              → MR 신규 진입
    """
    import time as _time
    _snap_ts = _time.time()
    intents: List[Intent] = []

    # ★ V10.27f: urgency 점수 로그 (5+ 시에만, 스팸 방지)
    _urg_log = _calc_urgency(st, snapshot)
    if _urg_log["urgency"] >= 5:
        _prev_urg = getattr(generate_all_intents, "_last_urg", 0)
        if abs(_urg_log["urgency"] - _prev_urg) >= 2 or _urg_log["urgency"] >= 10:
            print(f"[URGENCY] score={_urg_log['urgency']:.1f} "
                  f"(skew={_urg_log['skew']:.1%}, heavy_roi={_urg_log['heavy_avg_roi']:+.1f}%) "
                  f"heavy={_urg_log['heavy_side']}")
            generate_all_intents._last_urg = _urg_log["urgency"]

    # ★ V10.31b: 미장 전 포지션 정리 (최우선)
    _pmc_intents = plan_pre_market_clear(snapshot, st, system_state)
    if _pmc_intents:
        intents += _pmc_intents
        return intents  # Phase 발동 시 다른 intent 생성 차단

    _fc_intents = plan_force_close(snapshot, st, system_state, _bad_regime_active)
    intents += _fc_intents
    _fc_syms = {i.symbol for i in _fc_intents}

    intents += plan_tp1(snapshot, st, exclude_syms=_fc_syms)
    intents += plan_trim_trail(snapshot, st, exclude_syms=_fc_syms)
    intents += plan_trail_on(snapshot, st)
    # ★ V10.30: plan_dca 제거 — _place_dca_preorders(LIMIT)로 통일 (시장가/LIMIT 중복 방지)
    # intents += plan_dca(snapshot, st, cooldowns, system_state)
    # ★ V10.31b: 미장전 신규 진입 차단 (08:00-09:30 ET)
    if not system_state.get("_pmc_block_entry"):
        intents += plan_counter(snapshot, st, system_state)
        intents += plan_insurance_sh(snapshot, st, system_state)
        intents += plan_open(snapshot, st, cooldowns, system_state)
    for _i in intents:
        if _i.metadata is None:
            _i.metadata = {}
        _i.metadata["snap_ts"] = _snap_ts
    return intents


# ═════════════════════════════════════════════════════════════════
# ★ V10.27e: 글로벌 전략 state 영속화
# ═════════════════════════════════════════════════════════════════
def save_strategy_state(system_state: dict):
    """모듈 글로벌 → system_state (save_position_book 직전 호출)."""
    system_state["_open_dir_cd"] = _open_dir_cd
    system_state["_bad_regime_active"] = _bad_regime_active
    # ★ V10.29e: 분리 모듈 위임
    save_exit_state(system_state)
    save_counter_state(system_state)


def restore_strategy_state(system_state: dict):
    """system_state → 모듈 글로벌 (부팅 시 호출)."""
    global _open_dir_cd, _bad_regime_active
    _open_dir_cd = system_state.get("_open_dir_cd", {"buy": 0.0, "sell": 0.0})
    _bad_regime_active = system_state.get("_bad_regime_active", False)
    # ★ V10.29e: 분리 모듈 위임
    restore_exit_state(system_state)
    restore_counter_state(system_state)
    print(f"[RESTORE] strategy state: bad_regime={_bad_regime_active}")
    try:
        from v9.logging.logger_csv import log_system
        log_system("RESTORE", f"strategy bad_regime={_bad_regime_active}")
    except Exception:
        pass
