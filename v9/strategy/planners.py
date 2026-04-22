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
    # ★ V10.31e-4: FALLING_KNIFE_BARS/THRESHOLD 제거 (필터 비활성화)
    LONG_ONLY_SYMBOLS, SHORT_ONLY_SYMBOLS,
    OPEN_CORR_MIN,
    SYM_MIN_QTY, SYM_MIN_QTY_DEFAULT,
    DCA_ENTRY_BASED, DCA_ENTRY_ROI,
    DCA_ENTRY_ROI_BY_TIER,
    TP1_FIXED, HARD_SL_BY_TIER,  # ★ V10.31e-6: HEDGE_SIM용
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

# ★ V10.31d-3: 방향별 글로벌 진입 쿨다운 완전 제거 (Phase 3 dead code 정리)
# V10.31d에서 값=0으로 무력화했던 것을 변수/체크/세팅 전부 삭제.
# 이전 주석(V10.27~V10.31d) 기록은 CLAUDE.md 히스토리 참조.

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



# ═════════════════════════════════════════════════════════════════
# ★ V10.31e-4: Falling Knife Filter 제거
# v9.9에서 도입, 9일 실측으로 효과 없음 확인 후 삭제.
# 필터 있는 MR T3 FC 5.8% > 필터 없는 TREND 4.4% (무용).
# 설정(FALLING_KNIFE_BARS/THRESHOLD)은 config.py에 유지하되 비활성화 주석.
# ═════════════════════════════════════════════════════════════════


# ═════════════════════════════════════════════════════════════════
# ★ V10.29c: TREND COMPANION — MR 진입 시 반대 방향 추세 심볼 동시 진입
# ═════════════════════════════════════════════════════════════════
_trend_cooldown: Dict[str, float] = {}  # sym → next allowed ts
# ★ V10.31c: TREND_SCORE_SKIP 로그 스팸 방지 — 모듈 dict 사용
# (기존 setattr(plan_open, ...) 방식은 함수 리임포트 등으로 리셋되어 매 틱 로깅됨)
_TREND_SKIP_LOG_CD: Dict[str, float] = {}  # f"{scope}:{sym}" → next log allowed ts
_TREND_SKIP_LOG_CD_SEC = 300  # 심볼당 5분 1회

def _calc_trend_score(ohlcv_15m: list, ohlcv_1m: list = None) -> float:
    """추세 점수: EMA 이격 × 거래량 서지 × RSI 가중치.
    양수=상승 추세, 음수=하락 추세. ★ V10.31c: 후보 풀 진입 기준은 _TR_MIN=0.5,
    NOSLOT/COMP 발사 시 abs()가 1.0~2.0이면 블록됨 (애매한 트렌드 차단)."""
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
    # ★ V10.31j: 미장전 진입 차단 비활성 (사용자 결정, 주석처리)
    # ★ V10.31b: 미장전 신규 진입 차단
    # if system_state.get("_pmc_block_entry"):
    #     return intents
    long_targets  = list(getattr(snapshot, "global_targets_long",  None) or [])
    short_targets = list(getattr(snapshot, "global_targets_short", None) or [])
    # ★ Python UnboundLocalError 방지: 루프 안에서 재할당되는 변수 미리 초기화
    total_cap = _mr_available_balance(snapshot, st)  # ★ V10.31b: BC 노셔널 차감

    # ═══ V10.31u: PENDING HEDGE_COMP 발사 (MR fill 확인 후) ═══
    # V10.31u: TREND_COMP 제거 → 동일 심볼 반대 방향 CORE_HEDGE로 교체
    # 기존 TREND_COMP: 다른 심볼 반대 방향 추세 심볼 (-$30 순손실, 승률 낮음)
    # 변경 HEDGE_COMP: 동일 심볼 반대 방향 (HEDGE_SIM 결과 100% 승률 재현 목표)
    # 실측 04-21~22 HEDGE_SIM 13건 +$113 추정 vs TREND_COMP 실전 -$30
    _ptc = system_state.get("_pending_hedge_comp") or system_state.get("_pending_trend_comp")
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
            system_state.pop("_pending_hedge_comp", None)
            system_state.pop("_pending_trend_comp", None)
        elif _ptc_mr_filled:
            # MR fill 확인 → HEDGE_COMP 발사 (동일 심볼 반대 방향)
            _ptc_sym = _ptc["symbol"]
            _ptc_side = _ptc["side"]
            _ptc_cp = float((snapshot.all_prices or {}).get(_ptc_sym, _ptc.get("price", 0)))
            _ptc_qty = _ptc.get("qty", 0)
            if _ptc_cp > 0:
                _ptc_qty = (_ptc_qty * _ptc.get("price", _ptc_cp)) / _ptc_cp
            if _ptc_qty > 0 and _ptc_cp > 0:
                intents.append(Intent(
                    trace_id=_tid(),
                    intent_type=IntentType.OPEN,
                    symbol=_ptc_sym,
                    side=_ptc_side,
                    qty=_ptc_qty,
                    price=_ptc_cp,
                    reason=f"HEDGE_COMP(mr={_ptc_mr_sym})",
                    metadata={
                        "atr": 0,
                        "dca_targets": _ptc.get("dca_targets", []),
                        "positionSide": "LONG" if _ptc_side == "buy" else "SHORT",
                        # ★ V10.31u: entry_type=TREND → T3_3H 시간컷 적용 (3~4h 정리)
                        "entry_type": "TREND",
                        # ★ V10.31u: CORE_MR_HEDGE role — 기존 CORE_HEDGE와 구분
                        # 기존 CORE_HEDGE는 스큐 기반 자동 헷지 전용 (HEDGE 계열 가드 적용)
                        # CORE_MR_HEDGE는 MR과 동일 DCA/TP1/SL 로직, 다만 다른 이름으로 구분
                        # slot_manager에서 CORE_MR/CORE_BREAKOUT만 MR 슬롯 카운트 → CORE_MR_HEDGE 별도
                        "role": "CORE_MR_HEDGE",
                        "locked_regime": _ptc.get("regime", "LOW"),
                    },
                ))
                print(f"[HEDGE_FIRE] {_ptc_sym} {_ptc_side} (동일 심볼 반대방향) "
                      f"← MR {_ptc_mr_sym} filled (delay={_ptc_age:.0f}s)")
            system_state.pop("_pending_hedge_comp", None)
            system_state.pop("_pending_trend_comp", None)
    # ═══ END PENDING HEDGE_COMP ═══

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
    # ★ V10.31k: 같은 틱 내 이미 생성한 MR intent 카운트 (st 미갱신 상태에서 슬롯 초과 방지)
    _tick_new_long = 0
    _tick_new_short = 0
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
        # ★ V10.31k: 같은 틱 내 이미 생성한 intent도 합산 (st 미갱신 버그 수정)
        _real_long  = _slots_mr_pre.risk_long  + _tick_new_long
        _real_short = _slots_mr_pre.risk_short + _tick_new_short
        _can_long  = symbol in long_targets  and _real_long  < MAX_MR_PER_SIDE
        _can_short = symbol in short_targets and _real_short < MAX_MR_PER_SIDE
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
        # ★ V10.31c: TREND_MIN_SCORE 제거 (미사용 config)
        from v9.config import TREND_ENABLED, TREND_COOLDOWN_SEC, TREND_MAX_SCORE
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

        # ★ V10.31e-4: Falling Knife 필터 제거
        # 실측(9일, n=52+136): 필터 있는 MR이 T3 FC 비율 5.8% vs 필터 없는 TREND 4.4%.
        # 필터가 손실 방어 효과 없고 기회만 차단. MR의 "이격 구간 반대 진입" 철학과 정면 충돌.
        # 제거 후 효과는 log_trades.csv entry_type=MR 건수/실적 증가로 실측 검증.
        # 원복 필요 시 git log에서 `_is_falling_knife_long/short` 함수 + 이 호출 복원.

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
                # ★ V10.31d-3: _open_dir_cd 쿨다운 체크 제거
                _tr_opp_slots = _core_short if _tr_opp_side == "sell" else _core_long
                _sig_side_slots = _core_long if _trend_signal_side == "buy" else _core_short
                # ★ V10.31k: 같은 틱 내 새 MR intent 슬롯 반영
                _tr_opp_slots += (_tick_new_short if _tr_opp_side == "sell" else _tick_new_long)
                _sig_side_slots += (_tick_new_long if _trend_signal_side == "buy" else _tick_new_short)
                # ★ V10.31h: A 조건 — NOSLOT은 "비대칭 해소" 목적. 발사 후에도 비대칭 유지될 때만 의미.
                #   (_tr_opp_slots + 1) < _sig_side_slots 이어야 1개 추가 후에도 시그널 방향이 더 많음.
                #   같거나 역전이면 발사 의미 없음 — 누적 발사 양산 차단 (04/20 TIA 5건 다발 패턴).
                _a_ok = (_tr_opp_slots + 1) < _sig_side_slots
                if _tr_opp_slots < MAX_MR_PER_SIDE and _a_ok:
                    _tr_best_sym = None
                    _tr_best_score = 0
                    _tr_ohlcv_pool = snapshot.ohlcv_pool or {}
                    _tr_prices = snapshot.all_prices or {}
                    _tr_held = {s for s, ss in st.items() if isinstance(ss, dict)
                                for _, p in iter_positions(ss)
                                if isinstance(p, dict) and float(p.get("amt", 0) or 0) > 0}
                    _tr_entered = {i.symbol for i in intents}
                    # ★ V10.31q: TREND_COMP universe 필터링 (NOSLOT과 동일)
                    _tr_long_pool = set(getattr(snapshot, "global_targets_long", None) or [])
                    _tr_short_pool = set(getattr(snapshot, "global_targets_short", None) or [])
                    _tr_allowed_pool = _tr_long_pool if _tr_opp_side == "buy" else _tr_short_pool

                    for _tr_sym in _tr_ohlcv_pool:
                        if _tr_sym == symbol or _tr_sym in _tr_held or _tr_sym in _tr_entered:
                            continue
                        if _tr_sym == "BTC/USDT":
                            continue
                        # ★ V10.31q: universe 풀 (side별) 외부 차단
                        if _tr_sym not in _tr_allowed_pool:
                            continue
                        if _trend_cooldown.get(_tr_sym, 0) > now_ts:
                            continue
                        # ★ V10.31e: 심볼 실적 쿨다운
                        try:
                            from v9.strategy.symbol_stats import is_symbol_cooldown as _sc
                            if _sc(_tr_sym):
                                continue
                        except Exception:
                            pass
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
                    if _tr_opp_slots >= MAX_MR_PER_SIDE:
                        print(f"[TREND_SKIP] {symbol} (MR블록) → COMP {_tr_opp_side} 슬롯풀({_tr_opp_slots}/{MAX_MR_PER_SIDE})")
                    elif not _a_ok:
                        # ★ V10.31h: A 조건 위반 — 발사 후 균형/역전. 5분 1회 cooldown 로그.
                        _akey = f"NOSLOT_A:{_trend_signal_side}_{_sig_side_slots}_{_tr_opp_slots}"
                        _now_t = time.time()
                        if _now_t - _TREND_SKIP_LOG_CD.get(_akey, 0) > _TREND_SKIP_LOG_CD_SEC:
                            _TREND_SKIP_LOG_CD[_akey] = _now_t
                            print(f"[NOSLOT_SKIP_A] sig={_trend_signal_side} sig_slots={_sig_side_slots} "
                                  f"opp_slots={_tr_opp_slots} (발사 후 균형/역전 → 누적양산 차단)")
                            try:
                                from v9.logging.logger_csv import log_system
                                log_system("NOSLOT_SKIP_A",
                                           f"sig={_trend_signal_side} sig={_sig_side_slots} opp={_tr_opp_slots}")
                            except Exception: pass
            # ★ V10.30 FIX: trigger_side=None → MR 진입 코드 도달 차단
            continue
        # ★ V10.31d-3: _open_dir_cd 쿨다운 체크 제거
        # ★ V10.17 Rule A: Slot Balance Gate — 반대=0 AND 이쪽≥3 → 차단
        if trigger_side == "buy":
            if _open_shorts == 0 and _open_longs >= 3:
                continue
        else:
            if _open_longs == 0 and _open_shorts >= 3:
                continue

        if float(sym_st.get("open_fail_cooldown_until",   0.0)) > time.time(): continue
        if float(sym_st.get("reduce_fail_cooldown_until", 0.0)) > time.time(): continue

        # ★ V10.31e: 심볼별 실적 쿨다운 (최근 7일 손실 심볼 OPEN 차단)
        try:
            from v9.strategy.symbol_stats import is_symbol_cooldown
            if is_symbol_cooldown(symbol):
                continue
        except Exception:
            pass

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

        # ════════════════════════════════════════════════════════════
        # ★ V10.31c: TREND_FILTER_SIM — BTC 방향성 필터 shadow logging
        # 두 임계값 병렬 기록:
        #   Strict: 1h≤-1.5% OR 6h≤-4% OR dev_ma≤-3%
        #   Loose:  1h≤-0.7% OR 6h≤-2% OR dev_ma≤-1.5%
        # 실전 진입은 그대로 진행. MR 청산 시점에 sim ROI 계산해서 비교.
        # ════════════════════════════════════════════════════════════
        try:
            _fs_1h  = float(getattr(snapshot, "btc_1h_change", 0.0) or 0.0)
            _fs_6h  = float(getattr(snapshot, "btc_6h_change", 0.0) or 0.0)
            _fs_dev = float(getattr(snapshot, "dev_ma", 0.0) or 0.0)

            _fs_down_strict = (_fs_1h <= -0.015) or (_fs_6h <= -0.04) or (_fs_dev <= -3.0)
            _fs_up_strict   = (_fs_1h >=  0.015) or (_fs_6h >=  0.04) or (_fs_dev >=  3.0)
            _fs_down_loose  = (_fs_1h <= -0.007) or (_fs_6h <= -0.02) or (_fs_dev <= -1.5)
            _fs_up_loose    = (_fs_1h >=  0.007) or (_fs_6h >=  0.02) or (_fs_dev >=  1.5)

            _fs_strict_block = (trigger_side == "buy"  and _fs_down_strict) or \
                               (trigger_side == "sell" and _fs_up_strict)
            _fs_loose_block  = (trigger_side == "buy"  and _fs_down_loose)  or \
                               (trigger_side == "sell" and _fs_up_loose)

            if _fs_strict_block or _fs_loose_block:
                _fs_btc_dir = "DOWN" if (_fs_down_loose and trigger_side == "buy") else \
                              "UP"   if (_fs_up_loose   and trigger_side == "sell") else "FLAT"
                _fs_common = {
                    "ep": curr_p, "side": trigger_side, "ts": time.time(),
                    "btc_dir": _fs_btc_dir,
                    "btc_1h": _fs_1h, "btc_6h": _fs_6h, "dev_ma": _fs_dev,
                    "entry_type": entry_type_tag,
                }
                if _fs_strict_block:
                    _tfs_strict = system_state.setdefault("_trend_filter_sim_strict", {})
                    _tfs_strict[f"{symbol}:{trigger_side}"] = dict(_fs_common)
                if _fs_loose_block:
                    _tfs_loose = system_state.setdefault("_trend_filter_sim_loose", {})
                    _tfs_loose[f"{symbol}:{trigger_side}"] = dict(_fs_common)
                _fs_tags = []
                if _fs_strict_block: _fs_tags.append("STRICT")
                if _fs_loose_block:  _fs_tags.append("LOOSE")
                print(f"[TREND_FILTER_SIM] 📊 {symbol} {trigger_side} "
                      f"blocks={'+'.join(_fs_tags)} btc_dir={_fs_btc_dir} "
                      f"1h={_fs_1h*100:+.1f}% 6h={_fs_6h*100:+.1f}% devma={_fs_dev:+.1f}%")
                try:
                    from v9.logging.logger_csv import log_system
                    log_system("TREND_FILTER_SIM",
                        f"{symbol} {trigger_side} blocks={'+'.join(_fs_tags)} "
                        f"btc_dir={_fs_btc_dir} 1h={_fs_1h*100:+.1f}% "
                        f"6h={_fs_6h*100:+.1f}% devma={_fs_dev:+.1f}%")
                except Exception: pass
        except Exception as _fs_e:
            print(f"[TREND_FILTER_SIM] 기록 실패(무시): {_fs_e}")

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
        # ★ V10.31d-3: _open_dir_cd 세팅 제거 (쿨다운 자체 삭제)
        # ★ V10.27d: E30 슬롯 카운터 증가 (루프 내 중복 방지)
        if entry_type_tag == "15mE30":
            _active_e30 += 1
        # ★ V10.31k: 같은 틱 내 MR intent 카운터 증가 — 다음 심볼 슬롯 체크에 반영
        if trigger_side == "buy":
            _tick_new_long += 1
        else:
            _tick_new_short += 1

        # ★ V10.31u: TREND_COMP → HEDGE_COMP (동일 심볼 반대방향)
        # MR 진입 성공 시, 같은 심볼 반대 방향으로 CORE_HEDGE 동시 진입
        # 이전 TREND_COMP (다른 심볼 선정)는 -$30 순손실, ARB -$50 등 큰 손실
        # HEDGE_SIM 13건 +2% 모두 수익 → 실전 동일 로직 적용
        # entry_type=TREND → T3_3H 시간컷 적용, role=CORE_HEDGE (반대방향 식별용)

        if _trend_signal_side:
            _hc_opp_side = "sell" if trigger_side == "buy" else "buy"
            # ★ 같은 틱에서 이미 이 심볼 반대방향 intent 발사 예정이면 skip
            _hc_sym_entered = {i.symbol + ":" + i.side for i in intents}
            if symbol + ":" + _hc_opp_side in _hc_sym_entered:
                pass  # 이미 발사됨
            else:
                # ★ 반대방향 슬롯 여유 체크 (MR + HEDGE 동일 슬롯 사용 — CORE로 함께 카운트)
                _hc_opp_slots = _core_short if _hc_opp_side == "sell" else _core_long
                _hc_opp_slots += (_tick_new_short if _hc_opp_side == "sell" else _tick_new_long)
                if _hc_opp_slots >= MAX_MR_PER_SIDE:
                    print(f"[HEDGE_SKIP] {symbol} {_hc_opp_side} 슬롯풀({_hc_opp_slots}/{MAX_MR_PER_SIDE})")
                else:
                    # ★ T1 notional — MR과 동일 크기
                    _hc_grid = total_cap / GRID_DIVISOR * LEVERAGE
                    _hc_notional = _hc_grid * (DCA_WEIGHTS[0] / sum(DCA_WEIGHTS))
                    _hc_qty = _hc_notional / curr_p if curr_p > 0 and _hc_notional >= 10 else 0
                    if _hc_qty > 0:
                        _hc_dca_targets = _build_dca_targets(
                            curr_p, _hc_opp_side, _hc_grid, regime=_btc_regime)
                        system_state["_pending_hedge_comp"] = {
                            "symbol": symbol,   # ★ 동일 심볼
                            "side": _hc_opp_side,
                            "qty": _hc_qty,
                            "price": curr_p,
                            "mr_symbol": symbol,  # 자기 자신
                            "dca_targets": _hc_dca_targets,
                            "regime": _btc_regime,
                            "ts": time.time(),
                        }
                        print(f"[HEDGE_COMP] 📊 {symbol} {_hc_opp_side} "
                              f"notional=${_hc_notional:.0f} (MR {symbol} {trigger_side} 반대)")
                        try:
                            from v9.logging.logger_csv import log_system
                            log_system("HEDGE_COMP", f"{symbol} {_hc_opp_side} "
                                       f"notional={_hc_notional:.0f} ← MR {trigger_side}")
                        except Exception: pass
        elif TREND_ENABLED:
            # ★ V10.29e: TREND 시그널 미감지 사유 로그
            _ts_reasons = []
            if not _trend_signal_long and not _trend_signal_short:
                if not _mr_vs_ok: _ts_reasons.append("VS")
                if not _mr_mtf_ok: _ts_reasons.append("MTF")
                if not long_trig and not short_trig: _ts_reasons.append("ATR")
                if not micro_long_ok and not micro_short_ok: _ts_reasons.append("MICRO")
            print(f"[HEDGE_SKIP] {symbol} {trigger_side} → MR 시그널 없음({','.join(_ts_reasons) or 'N/A'})")

    # ★ V10.29e: TREND_NOSLOT — 루프 종료 후 최고 score 1개만 발사
    if _noslot_best:
        _ns = _noslot_best
        # ★ V10.31k: 최종 슬롯 재체크 — 루프 중 MR 진입으로 slot 도달했을 수 있음
        _final_slots = count_slots(st, role_filter="CORE_MR")
        _final_long  = _final_slots.risk_long  + _tick_new_long
        _final_short = _final_slots.risk_short + _tick_new_short
        _ns_side_full = ((_ns["side"] == "buy"  and _final_long  >= MAX_MR_PER_SIDE) or
                         (_ns["side"] == "sell" and _final_short >= MAX_MR_PER_SIDE))
        if _ns_side_full:
            try:
                from v9.logging.logger_csv import log_system
                log_system("NOSLOT_FINAL_BLOCK",
                           f"{_ns['sym']} {_ns['side']} long={_final_long} short={_final_short} (발사 직전 풀)")
            except Exception: pass
            print(f"[NOSLOT_FINAL_BLOCK] {_ns['sym']} {_ns['side']} "
                  f"long={_final_long} short={_final_short} (발사 직전 슬롯 풀 차단)")
            _noslot_best = None
    if _noslot_best:
        _ns = _noslot_best
        # ★ V10.31b: score 1.0~2.0 필터
        if 1.0 <= _ns["score"] < 2.0:
            # ★ V10.31c: 모듈 dict로 쿨다운 관리 (setattr 리셋 문제 fix)
            _skip_key = f"NOSLOT:{_ns['sym']}"
            _now_t = time.time()
            if _now_t - _TREND_SKIP_LOG_CD.get(_skip_key, 0) > _TREND_SKIP_LOG_CD_SEC:
                _TREND_SKIP_LOG_CD[_skip_key] = _now_t
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
                # ★ V10.31q: beta_by_sym 조회
                _ns_beta = (getattr(snapshot, "beta_by_sym", None) or {}).get(_ns["sym"], 0)
                print(f"[TREND_NOSLOT] ⚡ {_ns['sym']} {_ns['side']} score={_ns['score']:.1f} "
                      f"corr={_ns_corr:.2f} β={_ns_beta:.2f} ← {_ns['sig_sym']} (최고score 발사)")
                # ★ V10.31d-3: _open_dir_cd 세팅 제거
                try:
                    from v9.logging.logger_csv import log_system
                    log_system("TREND_NOSLOT",
                               f"{_ns['sym']} {_ns['side']} score={_ns['score']:.1f} "
                               f"corr={_ns_corr:.2f} β={_ns_beta:.2f} FIRE")
                except Exception: pass

    return intents


# ═════════════════════════════════════════════════════════════════
# ★ V10.31c: plan_dca 함수 제거 — V10.30부터 generate_all_intents에서 호출 중단.
# DCA는 _place_dca_preorders(LIMIT) 단일 경로로 통일. 호출부 0건 확인 후 제거.
# ═════════════════════════════════════════════════════════════════


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
    from v9.config import (HARD_SL_ATR_BASE,
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

        # ★ V10.31c: 모든 trail gap을 fixed 0.3로 통일 (ATR 분기 제거)
        _gap = 0.3

        # ── 발동 체크 (★ V10.31c: FLOOR 제거, peak 대비 gap만) ──
        _stop = _max - _gap
        _fire = False
        _reason = ""
        if roi_gross <= _stop:
            _fire = True
            _reason = f"TP_TRAIL(max={_max:.1f},gap={_gap:.2f},roi={roi_gross:.1f})"

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
    from v9.config import (TRIM_BLENDED_ROI_BY_TIER,
                           HARD_SL_ATR_BASE, calc_trim_qty,
                           calc_dynamic_trim_thresh)
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
        # ★ V10.31g: T3은 레짐 불문 LIMIT 선주문 경로로 위임
        #   근거: T3 trim threshold +0.5%는 매우 작은 이익 구간 — HIGH 변동성에서
        #   trail이 peak +0.5% 찍고 0.3% 하락만으로 발동, +0.2%만 먹고 이탈 → 다시
        #   T3 복귀 → T3_DEF/PRE_MKT/T3_8H 강제 청산으로 끌려가는 패턴.
        #   LIMIT은 +0.5% 정확 도달 시 maker(0.02%) 수수료로 깔끔히 tier 감소.
        _regime = _btc_vol_regime(snapshot)
        if _regime != "HIGH" or dca_level >= 3:
            # LOW/NORMAL or T3(any regime): _place_trim_preorders가 처리 → trail 정리 + skip
            # 배포 시점에 이미 HIGH+T3 trail 활성 상태로 남은 잔존 포지션도 여기서 자동 정리
            if p.get("trim_trail_active"):
                p["trim_trail_active"] = False
                p["trim_trail_max"] = 0.0
            continue

        # ── HIGH (T2만): trail 모드 ──
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
        # ★ V10.31j: worst_roi 기반 동적 임계 (T2 worst≤-2 → 0.5, 기본 1.5)
        _worst = float(p.get("worst_roi", 0.0) or 0.0)
        _threshold = calc_dynamic_trim_thresh(dca_level, _worst)

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

        # ★ V10.31c: 모든 trail gap을 fixed 0.3로 통일 (ATR 분기 제거)
        _gap = 0.3

        # ── 발동 체크 (★ V10.31c: FLOOR 제거, peak 대비 gap만) ──
        _stop = _max - _gap
        _fire = False
        _reason = ""

        if roi <= _stop:
            _fire = True
            _reason = f"TRIM_TRAIL(max={_max:.1f},gap={_gap:.2f},roi={roi:.1f})"

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

            # ★ V10.31c: 모든 trail gap fixed 0.3로 통일 (ATR 분기 제거)
            _trail_gap = 0.3
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
                # ★ V10.31o FIX: Binance min_qty 미달 잔량은 시도해도 FAIL 무한루프
                # FIL 0.1 케이스 — 0.0999...로 인식되어 17회 FAIL 반복
                # 해결: min_qty 미달 잔량은 강제 클리어 (Binance에서 자동 정리되거나 dust)
                from v9.config import SYM_MIN_QTY, SYM_MIN_QTY_DEFAULT
                _min_qty = SYM_MIN_QTY.get(symbol, SYM_MIN_QTY_DEFAULT)
                if _trail_qty < _min_qty * 0.9999:
                    # 잔량 강제 클리어 — 포지션북에서 제거
                    try:
                        from v9.execution.position_book import clear_position
                        from v9.logging.logger_csv import log_system
                        clear_position(st, symbol, p.get("side", "buy"))
                        log_system("RESIDUAL_FORCE_CLEAR",
                                   f"{symbol} {p.get('side','')} amt={_trail_qty} "
                                   f"< min_qty={_min_qty} (포지션북 강제 클리어)")
                    except Exception as _fc_err:
                        print(f"[RESIDUAL_FORCE_CLEAR] {symbol} 실패(무시): {_fc_err}")
                    continue  # intent 발사 안 함 (FAIL 루프 차단)
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

    # ── Phase 1 (08:00~08:30): 10분 단위 limit 재배치 (프리미엄 양보) ──
    # ★ V10.31e-8: 10분마다 프리미엄 0.15%씩 양보해 체결 확률 높임
    # T+0 (08:00):  +0.5%   (첫 배치)
    # T+10 (08:10): +0.35%  (0.15% 양보)
    # T+20 (08:20): +0.20%  (0.15% 양보)
    # T+30 (08:30): Phase 2 시장가 정리
    if BLOCK_START <= et_min < CLEAR_START:
        # 10분 스텝 계산 (0, 1, 2)
        _step = (et_min - BLOCK_START) // 10  # 0, 1, 2
        _step = max(0, min(_step, 2))
        _premium_bp = 0.005 - (_step * 0.0015)  # 0.50, 0.35, 0.20%

        step_key = f"_pmc_p1_{today_key}_s{_step}"
        if system_state.get(step_key):
            return intents

        # ★ 이전 스텝 limit 주문 취소 큐에 추가
        # _PENDING_LIMITS에서 is_pre_market_limit 플래그 달린 것만 필터
        try:
            from v9.execution.order_router import get_pending_limits
            from v9.strategy.strategy_core import _TRIM_CANCEL_QUEUE
            _cancelled = 0
            for _pl_oid, _pl_info in list(get_pending_limits().items()):
                if _pl_info.get("is_pre_market_limit"):
                    _TRIM_CANCEL_QUEUE.append({
                        "sym": _pl_info.get("sym", ""),
                        "oid": _pl_oid,
                    })
                    _cancelled += 1
            if _cancelled > 0:
                print(f"[PRE_MKT] step {_step} 이전 limit {_cancelled}건 취소 예약")
        except Exception as _ce:
            print(f"[PRE_MKT] 이전 limit 취소 실패(무시): {_ce}")

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
            close_price = curr_p * (1 + _premium_bp) if is_long else curr_p * (1 - _premium_bp)
            dca_level = int(p.get("dca_level", 1) or 1)

            # DCA 선주문 취소 (첫 스텝에서만 수행 — 이후 스텝은 이미 취소됨)
            if _step == 0:
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
                reason=f"PRE_MKT_P1_S{_step}_T{dca_level}(+{_premium_bp*100:.2f}%)",
                metadata={"pre_market_limit": True,
                          "is_pre_market_limit": True,  # ★ V10.31e-8: 스텝별 재배치용 플래그
                          "_expected_role": p.get("role", "")},
            ))

        if intents:
            system_state[step_key] = True
            print(f"[PRE_MKT] Phase 1 step {_step}: {len(intents)}건 "
                  f"limit +{_premium_bp*100:.2f}% 배치 "
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


# ═════════════════════════════════════════════════════════════════
# T3 8H CUT (V10.31f — T3 8시간 초과 시 단계적 정리)
# ═════════════════════════════════════════════════════════════════
def plan_t3_8h_cut(snapshot: "MarketSnapshot", st: Dict,
                   system_state: Dict) -> List[Intent]:
    """★ V10.31f: T3 포지션 8시간 초과 시 단계적 정리.

    실측 근거: T3 + hold >= 8h = 25건 -$519 (대부분 손실).
    T2/T1은 TRIM/TP1이 잘 처리 중이라 제외.

    단계:
      7h00 (step 0): limit +0.5% 유리방향 배치
      7h20 (step 1): 이전 limit 취소, +0.35% 재배치
      7h40 (step 2): 이전 limit 취소, +0.20% 재배치
      8h00 (step 3): 시장가 강제 정리

    유리한 방향:
      롱(buy) 포지션 청산 = sell @ curr × (1 + premium)
      숏(sell) 포지션 청산 = buy @ curr × (1 - premium)

    사용자 결정 (V10.31f):
      - 대상: T3만
      - 조건: 8h 초과는 무조건 (max_roi 조건 없음)
      - 일관성 우선: T3_DEF 활성 여부 무시
    """
    import time as _time

    intents: List[Intent] = []
    now_ts = _time.time()

    # 단계 경계 (초 단위)
    T_STEP0 = 7 * 3600              # 25200 (7h00)
    T_STEP1 = 7 * 3600 + 20 * 60    # 26400 (7h20)
    T_STEP2 = 7 * 3600 + 40 * 60    # 27600 (7h40)
    T_STEP3 = 8 * 3600              # 28800 (8h00)

    for symbol, p in _pos_items(st):
        # 헷지/트렌드 구조물 제외 (PRE_MKT와 동일)
        if p.get("role") in ("BC", "CB", "HEDGE", "SOFT_HEDGE",
                             "INSURANCE_SH", "CORE_HEDGE"):
            continue

        # ★ V10.31j: MR only — TREND는 plan_t3_3h_cut_trend가 3h~4h 더 빠른 컷 처리
        _entry_type = str(p.get("entry_type", "MR"))
        if _entry_type != "MR":
            continue

        # T3만 대상
        dca_level = int(p.get("dca_level", 1) or 1)
        if dca_level < 3:
            continue

        amt = float(p.get("amt", 0) or 0)
        if amt <= 0:
            continue

        curr_p = float((snapshot.all_prices or {}).get(symbol, 0.0))
        if curr_p <= 0:
            continue

        # hold 시간 계산 — 포지션 "time" 필드(OPEN 시점) 기준
        _open_ts = float(p.get("time", 0) or 0)
        if _open_ts <= 0:
            # time 없으면 안전하게 skip (이제 막 열린 포지션 과잉 정리 방지)
            continue
        hold_sec = now_ts - _open_ts

        if hold_sec < T_STEP0:
            continue

        # 현재 단계 판정
        if hold_sec < T_STEP1:
            cur_step = 0
            premium = 0.005   # 0.50%
        elif hold_sec < T_STEP2:
            cur_step = 1
            premium = 0.0035  # 0.35%
        elif hold_sec < T_STEP3:
            cur_step = 2
            premium = 0.0020  # 0.20%
        else:
            cur_step = 3
            premium = 0.0       # 시장가

        # 포지션의 현재 완료 단계 확인 (중복 방지)
        last_step = int(p.get("_t3_8h_step", -1))
        if cur_step <= last_step:
            continue  # 이미 해당 단계 실행됨

        is_long = p.get("side", "") == "buy"

        # 이전 단계 limit 주문 취소 (step 1, 2, 3에서만)
        if cur_step >= 1:
            try:
                from v9.execution.order_router import get_pending_limits
                from v9.strategy.strategy_core import _TRIM_CANCEL_QUEUE
                _cancelled = 0
                for _pl_oid, _pl_info in list(get_pending_limits().items()):
                    if (_pl_info.get("sym") == symbol
                            and _pl_info.get("is_t3_8h_limit")):
                        _TRIM_CANCEL_QUEUE.append({
                            "sym": symbol, "oid": _pl_oid,
                        })
                        _cancelled += 1
                if _cancelled > 0:
                    print(f"[T3_8H] {symbol} step {cur_step} 이전 limit "
                          f"{_cancelled}건 취소 예약")
            except Exception as _ce:
                print(f"[T3_8H] 이전 limit 취소 실패(무시): {_ce}")

        # Intent 생성
        if cur_step < 3:
            # Phase 1: 지정가 유리방향
            close_price = curr_p * (1 + premium) if is_long else curr_p * (1 - premium)
            intent_type = IntentType.CLOSE
            force_market = False
            reason = (f"T3_8H_S{cur_step}_h{hold_sec/3600:.1f}"
                      f"(+{premium*100:.2f}%)")
            _meta = {
                "is_t3_8h_limit": True,  # 재배치 추적용
                "_expected_role": p.get("role", ""),
            }
        else:
            # Phase 2: 시장가 강제
            close_price = curr_p
            intent_type = IntentType.FORCE_CLOSE
            force_market = True
            reason = f"T3_8H_MKT_h{hold_sec/3600:.1f}"
            _meta = {
                "force_market": True,
                "_expected_role": p.get("role", ""),
            }

        # DCA 선주문 취소 (첫 step에서만)
        if cur_step == 0:
            for _dt, _di in list(p.get("dca_preorders", {}).items()):
                if isinstance(_di, dict) and _di.get("oid"):
                    from v9.strategy.strategy_core import _TRIM_CANCEL_QUEUE
                    _TRIM_CANCEL_QUEUE.append({"sym": symbol, "oid": _di["oid"]})
            p["dca_preorders"] = {}

        intents.append(Intent(
            trace_id=_tid(),
            intent_type=intent_type,
            symbol=symbol,
            side="sell" if is_long else "buy",
            qty=amt,
            price=close_price,
            reason=reason,
            metadata=_meta,
        ))

        # 단계 완료 기록
        p["_t3_8h_step"] = cur_step

        print(f"[T3_8H] {symbol} {p.get('side', '')} T{dca_level} "
              f"step {cur_step} hold={hold_sec/3600:.2f}h: "
              f"{'시장가' if cur_step == 3 else f'+{premium*100:.2f}% limit'} "
              f"(qty={amt})")

    return intents


def plan_t3_3h_cut_trend(snapshot: "MarketSnapshot", st: Dict,
                         system_state: Dict) -> List[Intent]:
    """★ V10.31j: TREND T3 포지션 3시간 초과 시 단계적 정리.

    실측 근거 (OLD 500건):
      TREND_T3 회복률 — <3h 90% / ≥3h 38%
      TREND_T3 FC — 4h+ 13건 -$373 (주 손실 파이프라인)
      TREND_T3 TRIM — 4h+ 7건 +$35 (기회상실 확정)

    MR은 plan_t3_8h_cut (7h~8h) 별도 유지 — MR_T3 FC 전량 >12h 패턴.

    단계:
      3h00 (step 0): limit +0.5% 유리방향 배치
      3h20 (step 1): 이전 limit 취소, +0.35% 재배치
      3h40 (step 2): 이전 limit 취소, +0.20% 재배치
      4h00 (step 3): 시장가 강제 정리

    유리한 방향: plan_t3_8h_cut과 동일 (롱 sell @curr×(1+p), 숏 buy @curr×(1-p))

    사용자 결정 (V10.31j):
      - 대상: TREND T3만 (entry_type=="TREND")
      - 조건: 3h 초과는 무조건 (max_roi 조건 없음)
      - 일관성 우선: T3_DEF 활성 여부 무시
    """
    import time as _time

    intents: List[Intent] = []
    now_ts = _time.time()

    # 단계 경계 (초 단위)
    T_STEP0 = 3 * 3600              # 10800 (3h00)
    T_STEP1 = 3 * 3600 + 20 * 60    # 12000 (3h20)
    T_STEP2 = 3 * 3600 + 40 * 60    # 13200 (3h40)
    T_STEP3 = 4 * 3600              # 14400 (4h00)

    for symbol, p in _pos_items(st):
        # 헷지/트렌드 구조물 제외
        if p.get("role") in ("BC", "CB", "HEDGE", "SOFT_HEDGE",
                             "INSURANCE_SH", "CORE_HEDGE"):
            continue

        # ★ V10.31j: TREND only — MR은 plan_t3_8h_cut 담당
        _entry_type = str(p.get("entry_type", "MR"))
        if _entry_type != "TREND":
            continue

        # T3만 대상
        dca_level = int(p.get("dca_level", 1) or 1)
        if dca_level < 3:
            continue

        amt = float(p.get("amt", 0) or 0)
        if amt <= 0:
            continue

        curr_p = float((snapshot.all_prices or {}).get(symbol, 0.0))
        if curr_p <= 0:
            continue

        # hold 시간 계산
        _open_ts = float(p.get("time", 0) or 0)
        if _open_ts <= 0:
            continue
        hold_sec = now_ts - _open_ts

        if hold_sec < T_STEP0:
            continue

        # 현재 단계 판정
        if hold_sec < T_STEP1:
            cur_step = 0
            premium = 0.005   # 0.50%
        elif hold_sec < T_STEP2:
            cur_step = 1
            premium = 0.0035  # 0.35%
        elif hold_sec < T_STEP3:
            cur_step = 2
            premium = 0.0020  # 0.20%
        else:
            cur_step = 3
            premium = 0.0       # 시장가

        # 중복 방지 (plan_t3_8h_cut와 별도 필드 사용)
        last_step = int(p.get("_t3_3h_step", -1))
        if cur_step <= last_step:
            continue

        is_long = p.get("side", "") == "buy"

        # 이전 단계 limit 주문 취소 (step 1, 2, 3에서만)
        if cur_step >= 1:
            try:
                from v9.execution.order_router import get_pending_limits
                from v9.strategy.strategy_core import _TRIM_CANCEL_QUEUE
                _cancelled = 0
                for _pl_oid, _pl_info in list(get_pending_limits().items()):
                    if (_pl_info.get("sym") == symbol
                            and _pl_info.get("is_t3_3h_limit")):
                        _TRIM_CANCEL_QUEUE.append({
                            "sym": symbol, "oid": _pl_oid,
                        })
                        _cancelled += 1
                if _cancelled > 0:
                    print(f"[T3_3H] {symbol} step {cur_step} 이전 limit "
                          f"{_cancelled}건 취소 예약")
            except Exception as _ce:
                print(f"[T3_3H] 이전 limit 취소 실패(무시): {_ce}")

        # Intent 생성
        if cur_step < 3:
            close_price = curr_p * (1 + premium) if is_long else curr_p * (1 - premium)
            intent_type = IntentType.CLOSE
            force_market = False
            reason = (f"T3_3H_S{cur_step}_h{hold_sec/3600:.1f}"
                      f"(+{premium*100:.2f}%)")
            _meta = {
                "is_t3_3h_limit": True,
                "_expected_role": p.get("role", ""),
            }
        else:
            close_price = curr_p
            intent_type = IntentType.FORCE_CLOSE
            force_market = True
            reason = f"T3_3H_MKT_h{hold_sec/3600:.1f}"
            _meta = {
                "force_market": True,
                "_expected_role": p.get("role", ""),
            }

        # DCA 선주문 취소 (첫 step에서만)
        if cur_step == 0:
            for _dt, _di in list(p.get("dca_preorders", {}).items()):
                if isinstance(_di, dict) and _di.get("oid"):
                    from v9.strategy.strategy_core import _TRIM_CANCEL_QUEUE
                    _TRIM_CANCEL_QUEUE.append({"sym": symbol, "oid": _di["oid"]})
            p["dca_preorders"] = {}

        intents.append(Intent(
            trace_id=_tid(),
            intent_type=intent_type,
            symbol=symbol,
            side="sell" if is_long else "buy",
            qty=amt,
            price=close_price,
            reason=reason,
            metadata=_meta,
        ))

        p["_t3_3h_step"] = cur_step

        # ★ V10.31j: log_system 이벤트
        try:
            from v9.logging.logger_csv import log_system
            log_system(f"T3_3H_S{cur_step}",
                       f"{symbol} TREND hold={hold_sec/3600:.1f}h "
                       f"{'MKT' if cur_step==3 else f'+{premium*100:.2f}%'}")
        except Exception:
            pass

        print(f"[T3_3H] {symbol} {p.get('side', '')} T{dca_level} TREND "
              f"step {cur_step} hold={hold_sec/3600:.2f}h: "
              f"{'시장가' if cur_step == 3 else f'+{premium*100:.2f}% limit'} "
              f"(qty={amt})")

    return intents


# ═══════════════════════════════════════════════════════════════════
# ★ V10.31k: Portfolio TP (Peak Trail + Tier Gate, J+K 조합)
# ═══════════════════════════════════════════════════════════════════
def _ptp_get_drop_thresh(peak_pct: float) -> float:
    """V10.31k: Tiered drop 임계 — peak 높을수록 drop 허용폭 증가."""
    from v9.config import PTP_DROP_BY_PEAK
    for peak_min, drop_pct in PTP_DROP_BY_PEAK:
        if peak_pct >= peak_min:
            return drop_pct
    return 999.0  # peak < 1% → 사실상 미발동


def _ptp_session_date_kst(now_ts: float) -> str:
    """일일 세션 날짜 — ★ KST 09:00 (UTC 00:00) 기준.
    
    텔레그램 일일 수익률 리셋 시각과 통일 (_daily_pnl_report).
    PTP_SESSION_TZ_OFFSET_SEC = 0 → UTC 자정 기준 = KST 09:00 기준.
    """
    from v9.config import PTP_SESSION_TZ_OFFSET_SEC
    import time as _t
    return _t.strftime("%Y-%m-%d", _t.gmtime(now_ts + PTP_SESSION_TZ_OFFSET_SEC))


def _load_today_balance_stats(utc_day_start_ts: float) -> tuple:
    """★ V10.31l: log_balance.csv에서 오늘 UTC 00:00 이후 (start, peak) 복원.
    
    서버 타임존 UTC 가정 (log_balance.csv 시각 = UTC).
    오늘 UTC 00:00 (= KST 09:00) 이후 첫 레코드 = session_start,
    이후 모든 레코드 중 최대 = peak.
    
    Returns:
        (session_start, session_peak) — 데이터 없으면 (0.0, 0.0)
    """
    import os
    import time as _t
    from v9.config import LOG_DIR
    
    fpath = os.path.join(LOG_DIR, "log_balance.csv")
    if not os.path.exists(fpath):
        return (0.0, 0.0)
    
    # UTC day start → "YYYY-MM-DD HH:MM" 문자열 비교 (CSV 시각 포맷과 동일)
    boundary_str = _t.strftime("%Y-%m-%d %H:%M", _t.gmtime(utc_day_start_ts))
    
    session_start = 0.0
    session_peak = 0.0
    try:
        with open(fpath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(',')
                if len(parts) != 2:
                    continue
                ts_str = parts[0].strip()
                try:
                    bal = float(parts[1].strip())
                except (ValueError, IndexError):
                    continue
                if ts_str < boundary_str:
                    continue
                if session_start == 0.0:
                    session_start = bal
                if bal > session_peak:
                    session_peak = bal
    except Exception:
        return (0.0, 0.0)
    return (session_start, session_peak)


def _ptp_update_state(system_state: Dict, current_balance: float,
                      st: Dict, now_ts: float) -> bool:
    """PTP 상태 관리 — peak 추적 + 트리거 판정.
    
    Returns:
        True: PTP 활성 (plan_portfolio_tp 실행 필요)
        False: 미활성
    """
    from v9.config import PTP_PEAK_TRIG_PCT, PTP_AVG_TIER_GATE
    
    # 1) KST 09:00 (UTC 00:00) 세션 경계 + ★ V10.31l 재시작 시 복원
    # ★ V10.31m: 강제 복원 플래그 — 같은 날짜 안에 V10.31l 첫 가동 시 복원 트리거
    today_kst = _ptp_session_date_kst(now_ts)
    _force_restore = not system_state.get("_ptp_v31l_first_run_done")
    if (system_state.get("_ptp_session_date") != today_kst) or _force_restore:
        # ★ V10.31l: balance.csv에서 오늘 UTC 00:00 이후 start/peak 복원
        # 봇 재시작 시에도 "오늘 KST 09:00 대비 peak"를 연속 추적
        utc_day_start = int(now_ts // 86400) * 86400
        restored_start, restored_peak = _load_today_balance_stats(utc_day_start)
        
        system_state["_ptp_session_date"] = today_kst
        if restored_start > 0:
            # 복원 성공 — 오늘 UTC 자정 이후 레코드 존재 (재시작 케이스)
            system_state["_ptp_session_start"] = restored_start
            system_state["_ptp_peak_balance"] = max(restored_peak, current_balance)
            try:
                from v9.logging.logger_csv import log_system
                log_system("PTP_SESSION_RESTORE",
                           f"date={today_kst} start=${restored_start:.2f} "
                           f"peak=${system_state['_ptp_peak_balance']:.2f} "
                           f"curr=${current_balance:.2f} (balance.csv 복원)")
            except Exception:
                pass
        else:
            # 복원 실패 — balance.csv 없거나 오늘 레코드 없음 (신규 세션 또는 첫 부팅)
            system_state["_ptp_session_start"] = current_balance
            system_state["_ptp_peak_balance"] = current_balance
            try:
                from v9.logging.logger_csv import log_system
                log_system("PTP_SESSION_RESET",
                           f"date={today_kst} start=${current_balance:.2f} "
                           f"(새 세션 — 복원 데이터 없음)")
            except Exception:
                pass
        # 진행 중 상태 정리
        system_state.pop("_ptp_trigger_ts", None)
        system_state.pop("_ptp_last_step", None)
        # ★ V10.31m: 강제 복원 1회 마커 세팅 (이후엔 자정 전환 시에만 리셋)
        system_state["_ptp_v31l_first_run_done"] = True
    
    session_start = float(system_state.get("_ptp_session_start", current_balance) or current_balance)
    if session_start <= 0:
        return False
    
    peak = float(system_state.get("_ptp_peak_balance", current_balance) or current_balance)
    
    # 2) Peak 갱신
    if current_balance > peak:
        system_state["_ptp_peak_balance"] = current_balance
        peak = current_balance
    
    # 3) 이미 트리거 중이면 True 반환 (step 진행)
    if system_state.get("_ptp_trigger_ts"):
        return True
    
    # 4) Peak arm (J 조건): peak_gain ≥ 1%
    peak_gain_pct = (peak - session_start) / session_start * 100.0
    if peak_gain_pct < PTP_PEAK_TRIG_PCT:
        return False
    
    # 5) Drop 조건 (J: tiered)
    drop_pct = (peak - current_balance) / session_start * 100.0
    drop_thresh = _ptp_get_drop_thresh(peak_gain_pct)
    if drop_pct < drop_thresh:
        return False
    
    # 6) Tier gate (K): avg_dca_level ≥ 1.5
    tiers = []
    for _sym_p, _sym_pp in _pos_items(st):
        if _sym_pp.get("role") in ("BC", "CB", "HEDGE", "SOFT_HEDGE",
                                    "INSURANCE_SH", "CORE_HEDGE"):
            continue
        tier = int(_sym_pp.get("dca_level", 1) or 1)
        tiers.append(tier)
    
    if not tiers:
        return False  # 포지션 없으면 발동 불필요
    
    avg_tier = sum(tiers) / len(tiers)
    if avg_tier < PTP_AVG_TIER_GATE:
        # 안정 구간 (T1 대부분) — tier 리셋 불필요
        return False
    
    # 7) 트리거 확정
    system_state["_ptp_trigger_ts"] = now_ts
    system_state["_ptp_last_step"] = -1
    try:
        from v9.logging.logger_csv import log_system
        log_system("PTP_TRIGGER",
                   f"peak={peak_gain_pct:.2f}% drop={drop_pct:.2f}%p "
                   f"avg_tier={avg_tier:.2f} bal=${current_balance:.2f} "
                   f"pos={len(tiers)}")
        print(f"[PTP_TRIGGER] peak={peak_gain_pct:.2f}% drop={drop_pct:.2f}%p "
              f"avg_tier={avg_tier:.2f} positions={len(tiers)}")
    except Exception:
        pass
    return True


def plan_portfolio_tp(snapshot: "MarketSnapshot", st: Dict,
                       system_state: Dict) -> List[Intent]:
    """★ V10.31k: Portfolio TP 단계적 청산.
    
    트리거 조건 (J+K):
      (J-1) peak_gain ≥ 1%
      (J-2) drop ≥ f(peak) (tiered 0.3/0.4/0.5%p)
      (K)   avg_dca_level ≥ 1.5 (위험 축적 상태)
    
    단계 (T3_3H 패턴):
      step 0 (0-5min):  +0.20% limit
      step 1 (5-10min): +0.15% (이전 취소+재배치)
      step 2 (10-15min): +0.10%
      step 3 (15min+):  시장가 강제
    
    자연 쿨다운: step 3 완료 후 _session_start = current_balance
    → 또 1% 쌓여야 재발동. 명시 쿨다운 불필요.
    """
    import time as _time
    intents: List[Intent] = []
    now_ts = _time.time()
    
    # 상태 업데이트 + 트리거 판정
    current_balance = float(getattr(snapshot, "real_balance_usdt", 0) or 0)
    if current_balance <= 0:
        return []
    
    if not _ptp_update_state(system_state, current_balance, st, now_ts):
        return []
    
    # 단계 진행
    trigger_ts = float(system_state["_ptp_trigger_ts"])
    elapsed = now_ts - trigger_ts
    
    from v9.config import PTP_STEP_INTERVAL_SEC, PTP_PREMIUMS_BY_STEP
    
    T_STEP0 = PTP_STEP_INTERVAL_SEC          # 5min
    T_STEP1 = PTP_STEP_INTERVAL_SEC * 2      # 10min
    T_STEP2 = PTP_STEP_INTERVAL_SEC * 3      # 15min
    
    if elapsed < T_STEP0:
        cur_step = 0
        premium = PTP_PREMIUMS_BY_STEP.get(0, 0.002)
    elif elapsed < T_STEP1:
        cur_step = 1
        premium = PTP_PREMIUMS_BY_STEP.get(1, 0.0015)
    elif elapsed < T_STEP2:
        cur_step = 2
        premium = PTP_PREMIUMS_BY_STEP.get(2, 0.001)
    else:
        cur_step = 3
        premium = 0.0
    
    last_step = int(system_state.get("_ptp_last_step", -1))
    if cur_step <= last_step:
        return []
    
    # 이전 단계 PTP limit 취소 (step 1~3)
    if cur_step >= 1:
        try:
            from v9.execution.order_router import get_pending_limits
            from v9.strategy.strategy_core import _TRIM_CANCEL_QUEUE
            _cancelled = 0
            for _pl_oid, _pl_info in list(get_pending_limits().items()):
                if _pl_info.get("is_ptp_limit"):
                    _TRIM_CANCEL_QUEUE.append({
                        "sym": _pl_info.get("sym", ""), "oid": _pl_oid,
                    })
                    _cancelled += 1
            if _cancelled > 0:
                print(f"[PTP] step {cur_step} 이전 limit {_cancelled}건 취소 예약")
        except Exception as _ce:
            print(f"[PTP] 이전 limit 취소 실패(무시): {_ce}")
    
    # 전 포지션 intent 생성 (BC/CB/HEDGE 계열 제외)
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
        
        if cur_step < 3:
            close_price = curr_p * (1 + premium) if is_long else curr_p * (1 - premium)
            intent_type = IntentType.CLOSE
            reason = f"PTP_S{cur_step}(+{premium*100:.2f}%)"
            _meta = {
                "is_ptp_limit": True,
                "_expected_role": p.get("role", ""),
            }
        else:
            close_price = curr_p
            intent_type = IntentType.FORCE_CLOSE
            reason = f"PTP_MKT"
            _meta = {
                "force_market": True,
                "_expected_role": p.get("role", ""),
            }
        
        # DCA 선주문 취소 (첫 step에서만)
        if cur_step == 0:
            for _dt, _di in list(p.get("dca_preorders", {}).items()):
                if isinstance(_di, dict) and _di.get("oid"):
                    from v9.strategy.strategy_core import _TRIM_CANCEL_QUEUE
                    _TRIM_CANCEL_QUEUE.append({"sym": symbol, "oid": _di["oid"]})
            p["dca_preorders"] = {}
        
        intents.append(Intent(
            trace_id=_tid(),
            intent_type=intent_type,
            symbol=symbol,
            side="sell" if is_long else "buy",
            qty=amt,
            price=close_price,
            reason=reason,
            metadata=_meta,
        ))
    
    system_state["_ptp_last_step"] = cur_step
    
    # step 3 완료: 세션 리셋 (자연 쿨다운)
    if cur_step == 3:
        system_state["_ptp_session_start"] = current_balance  # 새 시작점
        system_state["_ptp_peak_balance"] = current_balance
        system_state.pop("_ptp_trigger_ts", None)
        system_state.pop("_ptp_last_step", None)
        try:
            from v9.logging.logger_csv import log_system
            log_system("PTP_COMPLETE",
                       f"bal=${current_balance:.2f} positions_closed={len(intents)}")
            print(f"[PTP] 완료 — 세션 리셋 bal=${current_balance:.2f}")
        except Exception:
            pass
    else:
        try:
            from v9.logging.logger_csv import log_system
            log_system(f"PTP_S{cur_step}",
                       f"premium={premium*100:.2f}% positions={len(intents)}")
            print(f"[PTP_S{cur_step}] +{premium*100:.2f}% limit × {len(intents)} positions")
        except Exception:
            pass
    
    return intents


def generate_all_intents(
    snapshot: MarketSnapshot,
    st: Dict,
    cooldowns: Dict,
    system_state: Dict,
) -> List[Intent]:
    """
    ★ V10.31c 실행 순서:
      1. plan_pre_market_clear → 미장전 포지션 정리 (ET 08:30, 최우선)
      2. plan_force_close       → HARD_SL / ZOMBIE / TIMECUT
      3. plan_tp1 / plan_trim_trail / plan_trail_on  → 청산 관리
      4. plan_counter / plan_insurance_sh / plan_open  → 신규 진입
    ★ DCA는 별도 경로: runner._place_dca_preorders (LIMIT 선주문)
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

    # ★ V10.31j: 미장 전 포지션 정리 비활성화 (사용자 결정, 주석처리 — 함수 정의는 유지)
    # _pmc_intents = plan_pre_market_clear(snapshot, st, system_state)
    # if _pmc_intents:
    #     intents += _pmc_intents
    #     return intents  # Phase 발동 시 다른 intent 생성 차단

    # ★ V10.31k: Portfolio TP (J안 — peak trail + tiered drop)
    # peak ≥ 1% + drop ≥ f(peak) (0.3/0.4/0.5%p tiered) → 전체 단계적 청산
    # K gate(avg_tier) 비활성 — config PTP_AVG_TIER_GATE=0.0
    _ptp_intents = plan_portfolio_tp(snapshot, st, system_state)
    if _ptp_intents:
        intents += _ptp_intents
        _ptp_syms = {i.symbol for i in _ptp_intents}
        # PTP 활성 시 다른 intent 생성 차단 (중복 방지)
        for _i in _ptp_intents:
            if _i.metadata is None:
                _i.metadata = {}
            _i.metadata["snap_ts"] = _snap_ts
        return intents

    # ★ V10.31f: T3 8h 컷 (MR only, V10.31j에서 조건 추가)
    _t3_8h_intents = plan_t3_8h_cut(snapshot, st, system_state)
    intents += _t3_8h_intents
    _t3_8h_syms = {i.symbol for i in _t3_8h_intents}

    # ★ V10.31j: T3 3h 컷 (TREND only)
    _t3_3h_intents = plan_t3_3h_cut_trend(snapshot, st, system_state)
    intents += _t3_3h_intents
    _t3_3h_syms = {i.symbol for i in _t3_3h_intents}

    _fc_intents = plan_force_close(snapshot, st, system_state, _bad_regime_active)
    intents += _fc_intents
    _fc_syms = {i.symbol for i in _fc_intents}
    # T3 시간 컷 대상 심볼은 FC/TP1/TRIM 중복 방지
    _exclude = _fc_syms | _t3_8h_syms | _t3_3h_syms

    intents += plan_tp1(snapshot, st, exclude_syms=_exclude)
    intents += plan_trim_trail(snapshot, st, exclude_syms=_exclude)
    intents += plan_trail_on(snapshot, st)
    # ★ V10.31c: plan_dca 호출 제거 완료 (함수 자체도 삭제됨)
    # ★ V10.31j: 미장전 진입 차단 비활성 (주석처리) — 항상 진입 허용
    # ★ V10.31b: 미장전 신규 진입 차단 (08:00-09:30 ET)
    # if not system_state.get("_pmc_block_entry"):
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
    # ★ V10.31d-3: _open_dir_cd 제거 (쿨다운 자체 삭제)
    system_state["_bad_regime_active"] = _bad_regime_active
    # ★ V10.29e: 분리 모듈 위임
    save_exit_state(system_state)
    save_counter_state(system_state)


def restore_strategy_state(system_state: dict):
    """system_state → 모듈 글로벌 (부팅 시 호출)."""
    global _bad_regime_active
    # ★ V10.31d-3: _open_dir_cd restore 제거
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
