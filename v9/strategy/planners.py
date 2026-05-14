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

# ★ V14.18 [05-14]: BTC 추세 윈도우 추적 (모듈 글로벌)
#   BTC 10m ≥ ±0.3% 발동 시점 + 20분간 추세 ON 유지
#   사용자 결정: "MR 진입은 BTC 트렌드 ON 시점 이후 20분간 기회 부여"
_BTC_TREND_WINDOW_UNTIL = 0.0
_BTC_TREND_WINDOW_DIRECTION = ""  # "up" | "down" | ""
from v9.risk.slot_manager import count_slots
from v9.execution.position_book import (
    get_p, set_p, iter_positions, is_active,
    get_pending_entry, set_pending_entry,
)
from v9.engines.hedge_core import (
    calc_skew,
)



# ═════════════════════════════════════════════════════════════════
# ═════════════════════════════════════════════════════════════════
# ★ V10.31b: MR 가용 잔고 (BC/CB 보유 노셔널 차감)
# ★ V10.31AM: BC/CB 차감 제거 — TREND off 상태라 마진 여유 충분, KILLSWITCH(margin_ratio)로 통합 관리
#   근거: 평상시 margin_ratio 30~45% (임계 80% 대비 여유 많음)
#         BC 노셔널 $400 차감 시 MR T3 노셔널 $1271 → $1121 (13% 축소) — 불필요한 손해
#         사용자 판단: "TREND 없으면 마진율 널널하니까 차감 말고 전체 마진율만 관리"
#   안전장치: KILLSWITCH_BLOCK_NEW_MR=0.80 / BLOCK_ALL_MR=0.85 / FREEZE_ALL_MR=0.90 그대로 유지
# ═════════════════════════════════════════════════════════════════
def _mr_available_balance(snapshot, st: Dict) -> float:
    """★ V10.31AM: BC/CB 차감 제거. real_balance_usdt 전체 반환.
    
    과거 (V10.31b~AL): TREND 활성 시 BC/CB 노셔널 만큼 MR 가용 잔고 차감 → 슬롯/마진 충돌 방지.
    현재 (V10.31AM): TREND off 상태에서 MR 사이즈 부당 축소 — 전체 잔고 기준 사용.
                    margin_ratio (Binance 실시간) 기반 KILLSWITCH가 마진 한도 관리.
    
    재활성 방법: 이 함수 내부만 아래 주석 블록으로 복구. 호출부 5곳 유지.
    """
    return float(getattr(snapshot, "real_balance_usdt", 0.0) or 0.0)
    # ─── 과거 BC/CB 차감 로직 (V10.31b~AL) — 롤백 시 주석 해제 ───
    # bal = float(getattr(snapshot, "real_balance_usdt", 0.0) or 0.0)
    # prices = getattr(snapshot, "all_prices", {}) or {}
    # bc_notional = 0.0
    # for sym, sym_st in (st or {}).items():
    #     if not isinstance(sym_st, dict):
    #         continue
    #     for _, p in iter_positions(sym_st):
    #         if not isinstance(p, dict):
    #             continue
    #         if p.get("role") in ("BC", "CB"):
    #             amt = float(p.get("amt", 0) or 0)
    #             cp = float(prices.get(sym, 0) or 0)
    #             if amt > 0 and cp > 0:
    #                 bc_notional += amt * cp
    # return max(bal - bc_notional, bal * 0.3)  # 최소 30% 보장


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
    # ★ V10.31x: HIGH 임계 대폭 상향 (0.70/0.73/0.67 → 0.90/0.92/0.85)
    # 근거: 실측 HIGH 6블록 전부 BTC 1h 변동 ≤1% (평범한 변동도 HIGH 분류됨)
    # 사용자 의도: "진짜 폭등 폭락만 HIGH" → 상위 10% 극단 변동만 분류
    # 영향: trim_trail, URGENCY_DCA 등 HIGH 전용 로직이 진짜 예외 상황만 발동
    # 빈도: 기존 14% → 1~2% 추정
    if _regime_last == "LOW":
        new = "LOW" if _p < 0.60 else ("NORMAL" if _p < 0.90 else "HIGH")
    elif _regime_last == "NORMAL":
        new = "LOW" if _p < 0.50 else ("NORMAL" if _p < 0.92 else "HIGH")
    elif _regime_last == "HIGH":
        new = "LOW" if _p < 0.50 else ("NORMAL" if _p < 0.85 else "HIGH")
    else:
        new = "LOW" if _p < 0.55 else ("NORMAL" if _p < 0.90 else "HIGH")

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
        # ★ V10.31x: REGIME 전환 점수 파일 로깅 (임계 조정 효과 검증용)
        try:
            from v9.logging.logger_csv import log_system
            log_system("REGIME_CHANGE",
                       f"{_regime_last}->{new} score={_p:.3f} "
                       f"5m={pctl_5m:.2f} 15m={pctl_15m:.2f} 1h={pctl_1h:.2f}")
        except Exception: pass

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
    # ★ V10.31AM3 hotfix-4: T3 다단계 디펜스
    calc_t3_defense_action, T3_DEFENSE_LADDER,
    calc_t2_defense_action, T2_DEFENSE_LADDER,
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
_ATR_BASE = 3.5           # ★ V14.19 [05-14]: 3.0 → 3.5 (사용자 결정 — 진입 빈도 ↓, 변동성 큰 sym만)
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
# ★ V10.31AN-hf1 [04-30]: DCA_ROI_TRIGGERS deprecated — DCA_ENTRY_ROI_BY_TIER로 통일
#   배경: V10.31AM3 hf-4 배포 시 DCA_ENTRY_ROI_BY_TIER만 변경, _build_dca_targets는
#         이 stale dict 사용 → trim 후 dca_targets 재생성에서 stale -1.8/-3.6 사용
#         → 새 DCA 거리 반영 안 됨 (잠재 일관성 버그)
#   수정: _build_dca_targets에서 DCA_ENTRY_ROI_BY_TIER 직접 참조
DCA_ROI_TRIGGERS = {2: -1.8, 3: -3.6}  # ★ DEPRECATED — 호환성 위해 유지, 실제 미사용

def _wider_regime(a: str, b: str) -> str:
    """호환용 stub — DCA 거리 통일로 항상 LOW."""
    return "LOW"

def _build_dca_targets(
    entry_p: float, side: str, grid_notional: float,
    regime: str = "LOW",
) -> list:
    """★ V10.29b: DCA 타겟 — T2만 (V10.31AO: T3 제거).
    ★ V10.31AO: DCA_WEIGHTS=[33,67], T2 단일 DCA. T3 진입 안 함.
    """
    dca_w   = DCA_WEIGHTS  # [33, 67]
    total_w = sum(dca_w)
    targets = []
    # ★ V10.31AO: T2만 처리 (T3 제거)
    for tier in DCA_ENTRY_ROI_BY_TIER.keys():  # {2: -1.0} → tier=2만
        roi_trig = DCA_ENTRY_ROI_BY_TIER[tier]
        dist = abs(roi_trig) / 100 / LEVERAGE
        target_p = entry_p * (1.0 - dist) if side == "buy" else entry_p * (1.0 + dist)
        # T2 비중은 dca_w[1] (T1 다음)
        w_idx = tier - 1  # tier=2 → w_idx=1
        if w_idx < len(dca_w):
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
    양수=상승 추세, 음수=하락 추세. ★ V14.4 [05-06]: 후보 풀 진입 기준은 _TR_MIN=0.5,
    1.0~2.0 차단 제거 (사용자 결정 [05-06])."""
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
    # ★ V10.31AM3 hotfix-9: PTP 후 진입 cooldown (사용자 결정 [04-27] "이벤트 끝난 두시간")
    #   PTP_COMPLETE 시점에 _ptp_entry_cooldown_until 세팅됨 (planners.py:2939 부근)
    #   해당 시각 이전엔 모든 OPEN intent 차단 (1차/2차 추세 cover)
    _pec_until = float(system_state.get("_ptp_entry_cooldown_until", 0.0) or 0.0)
    if _pec_until > 0 and time.time() < _pec_until:
        _pec_remain = _pec_until - time.time()
        # 1분에 1회만 로그 (스팸 방지)
        _pec_last_log = getattr(plan_open, "_pec_last_log_ts", 0)
        if time.time() - _pec_last_log >= 60:
            print(f"[PTP_ENTRY_LOCK] OPEN 차단 — PTP 후 cooldown {_pec_remain/60:.0f}분 남음")
            plan_open._pec_last_log_ts = time.time()
        return intents
    # ★ V10.31AO [04-30]: HARD_SL 쿨다운 — 추세장 연쇄 사망 차단
    #   사용자 결정: 1시간 내 HARD_SL N건 이상이면 30분 신규 진입 차단
    #   _hard_sl_history는 hedge_engine.py(HARD_SL_T*) + planners.py(T2_DEF_SL/HARD_SL)에서 기록
    try:
        from v9.config import (HARDSL_COOLDOWN_SEC, HARDSL_COOLDOWN_WINDOW_SEC,
                                HARDSL_COOLDOWN_MIN_COUNT)
        _now_hsl = time.time()
        _hsl_history = system_state.get("_hard_sl_history", []) or []
        # 윈도우 내 HARD_SL 건수
        _recent_hsl = [h for h in _hsl_history
                       if (_now_hsl - float(h.get("ts", 0) or 0)) < HARDSL_COOLDOWN_WINDOW_SEC]
        # 최근 HARD_SL 시각 (가장 최근)
        _last_hsl_ts = max((float(h.get("ts", 0) or 0) for h in _recent_hsl), default=0.0)
        _hsl_age = _now_hsl - _last_hsl_ts if _last_hsl_ts > 0 else 99999
        # 차단 조건: 윈도우 내 N건 이상 + 마지막 HARD_SL 후 쿨다운 시간 미경과
        if (len(_recent_hsl) >= HARDSL_COOLDOWN_MIN_COUNT 
                and _hsl_age < HARDSL_COOLDOWN_SEC):
            _hsl_last_log = getattr(plan_open, "_hsl_last_log_ts", 0)
            if _now_hsl - _hsl_last_log >= 60:  # 1분 1회 로그
                print(f"[HARDSL_COOLDOWN] OPEN 차단 — 최근 {HARDSL_COOLDOWN_WINDOW_SEC//60}분 내 "
                      f"HARD_SL {len(_recent_hsl)}건, 마지막 후 {_hsl_age/60:.1f}분 경과 "
                      f"(쿨다운 {HARDSL_COOLDOWN_SEC//60}분)")
                plan_open._hsl_last_log_ts = _now_hsl
            return intents
    except Exception as _hsl_e:
        # 안전: 쿨다운 체크 실패 시 정상 진행
        pass
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
                    reason=f"TREND_COMP(mr={_ptc_mr_sym})",
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
                print(f"[TREND_FIRE] {_ptc_sym} {_ptc_side} (다른 sym 추세) "
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

        # ── (4-b) RSI 트리거 (5m RSI6 기준) ★ V14.19: 14 → 6 (사용자 결정 — 빠른 진입)
        closes_5m_rsi = [float(x[4]) for x in ohlcv_5m]
        rsi5_now  = calc_rsi(closes_5m_rsi, period=6) if len(closes_5m_rsi) >= 7 else 50.0
        rsi5_prev = calc_rsi(closes_5m_rsi[:-1], period=6) if len(closes_5m_rsi) >= 8 else 50.0
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

        # ★ V14.14 [05-06]: TREND_DIRECT 알파 — 사용자 결정 [05-06]
        #   알파 정의: "RSI 극단 + TREND 반대 = 추세 추종 진입 (mean reversion 시그널 무시)"
        #     RSI ≥ 65 + TREND == "UP"  → BUY (counter to MR signal)
        #     RSI ≤ 35 + TREND == "DOWN" → SELL (counter to MR signal)
        #   1회 즉시 발동 (V14.13 슬롯풀+ROI 조건 폐기)
        #   MR 진입은 별도 차단 (MR_ENABLED=False)
        #   데이터 검증: 174건 +2.71% 평균, WR 79%, 합 +471% (1h hold sim)
        # 트리거: 시그널 방향 + 정반대 추세
        _td_trigger = False
        _td_entry_side = None
        # TREND 라벨 직접 계산 (line 1159와 동일 로직, NOSLOT 분기 시점에 미세팅이라)
        if ema_20_5m > 0 and ema_20_15m > 0:
            _td_trend = "UP" if ema_20_5m > ema_20_15m * 1.002 else ("DOWN" if ema_20_5m < ema_20_15m * 0.998 else "FLAT")
        else:
            _td_trend = "FLAT"
        # ★ V14.18 [05-14]: BTC 추세 윈도우 — 사용자 결정 "20분 윈도우"
        #   기존 V14.17: 매 cycle 즉시 btc_10m_change 비교 (시점 매칭, 매우 좁음)
        #   변경 V14.18: BTC 10m ≥ ±0.3% 발동 후 20분 유지
        #     발동 시: 윈도우 만료 시각 = now + 20분, 방향 기록
        #     20분 내 BTC 평탄으로 reverse해도 윈도우 유지
        #     20분 후 자동 종료
        #   효과: 진입 매칭 시기 확장 (5분 → 25분, ~5배)
        _btc_10m = float(getattr(snapshot, "btc_10m_change", 0.0) or 0.0)
        global _BTC_TREND_WINDOW_UNTIL, _BTC_TREND_WINDOW_DIRECTION
        _now_ts_btc = time.time()
        if _btc_10m >= 0.003:
            _BTC_TREND_WINDOW_UNTIL = _now_ts_btc + 1200  # 20분
            _BTC_TREND_WINDOW_DIRECTION = "up"
        elif _btc_10m <= -0.003:
            _BTC_TREND_WINDOW_UNTIL = _now_ts_btc + 1200
            _BTC_TREND_WINDOW_DIRECTION = "down"
        _btc_window_active = _now_ts_btc < _BTC_TREND_WINDOW_UNTIL
        _btc_up_ok = _btc_window_active and _BTC_TREND_WINDOW_DIRECTION == "up"
        _btc_down_ok = _btc_window_active and _BTC_TREND_WINDOW_DIRECTION == "down"
        # SHORT 시그널 + UP 추세 + BTC UP → BUY 진입
        if (_mr_signal_short and short_trig and micro_short_ok 
                and rsi5_now >= 65 and _td_trend == "UP"
                and _btc_up_ok):  # ★ V14.18: 윈도우 기반
            _td_trigger = True
            _td_entry_side = "buy"
        # LONG 시그널 + DOWN 추세 + BTC DOWN → SELL 진입
        elif (_mr_signal_long and long_trig and micro_long_ok 
                and rsi5_now <= 35 and _td_trend == "DOWN"
                and _btc_down_ok):  # ★ V14.18: 윈도우 기반
            _td_trigger = True
            _td_entry_side = "sell"
        
        if _td_trigger:
            # 같은 sym 진입 (시그널 잡힌 sym 그대로, 단 반대 방향)
            # held/intent 체크
            _td_held = {s for s, ss in st.items() if isinstance(ss, dict)
                        for _, p in iter_positions(ss)
                        if isinstance(p, dict) and float(p.get("amt", 0) or 0) > 0}
            _td_entered = {i.symbol for i in intents}
            if symbol in _td_held or symbol in _td_entered:
                pass  # 이미 진입 중이면 skip
            elif _trend_cooldown.get(symbol, 0) > now_ts:
                pass  # 쿨다운
            else:
                # ★ V14.14-hf1 [05-12]: universe pool 체크 완화 (양방향 OR)
                _td_long_pool = set(getattr(snapshot, "global_targets_long", None) or [])
                _td_short_pool = set(getattr(snapshot, "global_targets_short", None) or [])
                _td_allowed = _td_long_pool | _td_short_pool  # 합집합 (OR)
                
                # ★ V14.15-hf3 [05-12]: corr/score 필터 추가 — 사용자 결정 [05-12]
                #   문제: V14.14 진입 시점 분석 — score=0.0, corr 0.24~0.50 (BTC 무관 노이즈)
                #   해결: corr ≥ 0.6 (BTC 동조) + |score| ≥ 0.5 (추세 강도)
                #   어제 시뮬 (+471%) 조건과 일치 (corr 0.6+ universe)
                _td_corr = (getattr(snapshot, "correlations", None) or {}).get(symbol, 0)
                _td_pool = snapshot.ohlcv_pool.get(symbol, {}) if snapshot.ohlcv_pool else {}
                _td_15m = _td_pool.get("15m", [])
                _td_score = 0.0
                if len(_td_15m) >= 35:
                    try:
                        _td_score = _calc_trend_score(_td_15m)
                    except Exception:
                        _td_score = 0.0
                
                # 필터 1: corr ≥ 0.6 (사용자 결정 — config OPEN_CORR_MIN=0.50과 별개)
                _td_corr_ok = _td_corr >= 0.6
                # 필터 2: score 방향 일치 + |score| ≥ 0.5
                _td_score_ok = False
                if _td_entry_side == "buy" and _td_score >= 0.5:
                    _td_score_ok = True
                elif _td_entry_side == "sell" and _td_score <= -0.5:
                    _td_score_ok = True
                
                if not _td_corr_ok:
                    # corr 부족 → skip
                    try:
                        from v9.logging.logger_csv import log_system
                        log_system("TREND_DIRECT_SKIP", 
                                   f"{symbol} {_td_entry_side} corr={_td_corr:.2f} < 0.6")
                    except Exception: pass
                elif not _td_score_ok:
                    # score 부족 → skip
                    try:
                        from v9.logging.logger_csv import log_system
                        log_system("TREND_DIRECT_SKIP", 
                                   f"{symbol} {_td_entry_side} |score|={abs(_td_score):.2f} < 0.5 (score={_td_score:.2f})")
                    except Exception: pass
                elif symbol in _td_allowed:
                    # _noslot_best 세팅 (기존 NOSLOT 발사 로직 재사용)
                    if _noslot_best is None:
                        _noslot_best = {
                            "sym": symbol,
                            "side": _td_entry_side,
                            "score": abs(_td_score),  # ★ V14.15-hf3: 진짜 score 기록
                            "sig_sym": symbol,
                            "size_mult": 1.0,  # ★ V14.14: T1 풀사이즈 (그리드 100%)
                            "exposure_roi": 0.0,
                            "trigger_type": "TREND_DIRECT",
                            "td_trend": _td_trend,
                            "td_rsi": rsi5_now,
                            "td_corr": _td_corr,
                        }
        # ★ V14.14 [05-06]: V14.13 슬롯풀+ROI 트리거 모두 폐기 (위 V14.14 TREND_DIRECT 분기로 대체)
        # ★ V14.17 [05-13]: MR hedge 알파 — 사용자 결정
        #   알파 정의: "BTC 추세 시기 + V14.14 진입 반대 방향 MR 시그널만 진입"
        #     BTC UP 시기 (V14.14 BUY 가능) → MR은 SHORT 시그널(RSI 65+)만 받음 → 시그널대로 SELL 진입
        #     BTC DOWN 시기 (V14.14 SELL 가능) → MR은 LONG 시그널(RSI 35-)만 받음 → 시그널대로 BUY 진입
        #   "각자 유리한 시점에 진입" — 동시 진입 X (같은 sym 충돌 시 V14.14 우선)
        #   진입 방향: MR 시그널대로 (V14.16 사양 유지, V14.14의 반대 방향 컨셉 아님)
        #   데이터 비판: BTC UP + SELL 시그널대로 = -2.77% (5/6 데이터 174건, 명백한 음수)
        #               단 사용자 결정 받음. 1주 운영 데이터로 진짜 EV 측정.
        if trigger_side is not None:
            # 트렌드 시기 확인 (BTC 10m 윈도우 활성)
            if not (_btc_up_ok or _btc_down_ok):
                # 윈도우 만료 또는 미발동 → MR 차단
                _remaining = max(0, _BTC_TREND_WINDOW_UNTIL - _now_ts_btc)
                try:
                    from v9.logging.logger_csv import log_system
                    log_system("MR_SKIP_BTC", 
                               f"{symbol} {trigger_side} btc_10m={_btc_10m*100:.2f}% (윈도우 만료, 남은:{int(_remaining)}s)")
                except Exception: pass
                continue
            
            # BTC 방향과 시그널 방향 매칭
            # BTC UP → SHORT 시그널(trigger_side=sell)만 허용 (V14.14는 BUY 진입)
            # BTC DOWN → LONG 시그널(trigger_side=buy)만 허용 (V14.14는 SELL 진입)
            _mr_ok = False
            if _btc_up_ok and trigger_side == "sell":
                _mr_ok = True
            elif _btc_down_ok and trigger_side == "buy":
                _mr_ok = True
            
            if not _mr_ok:
                # BTC 방향과 일치하는 시그널 (V14.14가 잡을 영역) → MR skip
                try:
                    from v9.logging.logger_csv import log_system
                    log_system("MR_SKIP_BTC", 
                               f"{symbol} {trigger_side} btc_10m={_btc_10m*100:.2f}% (V14.14 영역)")
                except Exception: pass
                continue
            
            # V14.14가 같은 sym에 발사 중이면 MR skip (충돌 방지)
            if _noslot_best is not None and _noslot_best.get("sym") == symbol:
                try:
                    from v9.logging.logger_csv import log_system
                    log_system("MR_SKIP_BTC", 
                               f"{symbol} {trigger_side} (V14.14 같은 sym 우선)")
                except Exception: pass
                continue
            
            # MR 진입 허용 (시그널대로, trigger_side 유지)
            # 아래 일반 MR 진입 코드로 흘러감
        # 일반 MR 진입 코드로 도달 X (위 continue로 차단)
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
            _ema_gap_pct = (ema_20_5m - ema_20_15m) / ema_20_15m  # 기울기 정량화
        else:
            _trend_tag = "FLAT"
            _ema_gap_pct = 0.0
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

            # ★ V10.31AM3 hf-10: 모든 OPEN intent에 BTC 컨텍스트 기록 (사용자 결정 (3))
            #   기존 TREND_FILTER_SIM은 STRICT/LOOSE block 시점만 (편향).
            #   본 로깅은 모든 진입에 기록 → 1주 후 STRICT/LOOSE/ema_gap/조합 정확도 비교 가능.
            try:
                _strict_block_eval = ((trigger_side == "buy"  and (_fs_1h <= -0.015 or _fs_6h <= -0.04 or _fs_dev <= -3.0)) or
                                       (trigger_side == "sell" and (_fs_1h >=  0.015 or _fs_6h >=  0.04 or _fs_dev >=  3.0)))
                _loose_block_eval = ((trigger_side == "buy"  and (_fs_1h <= -0.007 or _fs_6h <= -0.02 or _fs_dev <= -1.5)) or
                                      (trigger_side == "sell" and (_fs_1h >=  0.007 or _fs_6h >=  0.02 or _fs_dev >=  1.5)))

                # ★ V10.31AM3 hotfix-21: universe β/corr + 5분 vol_ratio 기록
                #   universe_beta는 hf-21에서 시간축 50h → 3h 변경 (snapshot.beta_by_sym에 저장됨)
                #   universe_corr는 24h universe selection 기준 (snapshot.correlations)
                #   vol_ratio_5m는 로그 전용 — "위아래로 튀는 알트 차단" 가설 검증용
                _univ_beta = float(getattr(snapshot, "beta_by_sym", {}).get(symbol, 0.0) or 0.0)
                _univ_corr = float(getattr(snapshot, "correlations", {}).get(symbol, 0.0) or 0.0)
                _vol_ratio_5m = 0.0
                try:
                    # 1m × 5봉 vol_ratio (snapshot.ohlcv_pool — 추가 fetch 0)
                    _ohlcv_pool = getattr(snapshot, "ohlcv_pool", {}) or {}
                    _btc_1m = (_ohlcv_pool.get("BTC/USDT", {}) or {}).get("1m", []) or []
                    _alt_1m = (_ohlcv_pool.get(symbol, {}) or {}).get("1m", []) or []
                    if len(_btc_1m) >= 5 and len(_alt_1m) >= 5:
                        # 마지막 5봉 close로 1m return std
                        _btc_closes = [float(c[4]) for c in _btc_1m[-6:] if c[4]]  # 6 close → 5 return
                        _alt_closes = [float(c[4]) for c in _alt_1m[-6:] if c[4]]
                        if len(_btc_closes) >= 6 and len(_alt_closes) >= 6:
                            import math
                            _btc_lr = [math.log(_btc_closes[i] / _btc_closes[i-1])
                                       for i in range(1, len(_btc_closes)) if _btc_closes[i-1] > 0]
                            _alt_lr = [math.log(_alt_closes[i] / _alt_closes[i-1])
                                       for i in range(1, len(_alt_closes)) if _alt_closes[i-1] > 0]
                            if _btc_lr and _alt_lr:
                                _btc_mean = sum(_btc_lr) / len(_btc_lr)
                                _alt_mean = sum(_alt_lr) / len(_alt_lr)
                                _btc_var = sum((x - _btc_mean)**2 for x in _btc_lr) / len(_btc_lr)
                                _alt_var = sum((x - _alt_mean)**2 for x in _alt_lr) / len(_alt_lr)
                                _btc_std = math.sqrt(_btc_var) if _btc_var > 0 else 0
                                _alt_std = math.sqrt(_alt_var) if _alt_var > 0 else 0
                                if _btc_std > 1e-9:
                                    _vol_ratio_5m = _alt_std / _btc_std
                except Exception:
                    pass  # vol_ratio 계산 실패 — 0 fallback

                from v9.logging.logger_csv import log_btc_context as _lbc
                _lbc(
                    trace_id=str(int(now_ts)),
                    symbol=symbol,
                    side=trigger_side,
                    entry_type=entry_type_tag,
                    btc_price=float(getattr(snapshot, "btc_price", 0) or 0),
                    btc_1h_change=_fs_1h,
                    btc_6h_change=_fs_6h,
                    btc_dev_ma=_fs_dev,
                    ema_gap_pct=_ema_gap_pct,
                    trend_tag=_trend_tag,
                    regime=_btc_regime,
                    regime_score=float(_regime_ema_pctl) if _regime_ema_pctl is not None else 0.5,
                    strict_block=_strict_block_eval,
                    loose_block=_loose_block_eval,
                    universe_beta=_univ_beta,    # ★ hf-21
                    universe_corr=_univ_corr,    # ★ hf-21
                    vol_ratio_5m=_vol_ratio_5m,  # ★ hf-21 (로그 전용)
                )
            except Exception as _lbc_e:
                pass  # 로깅 실패 silent

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
                # ★ V14.19 [05-14]: MR 진입을 V14.14 청산 로직으로 통합 — 사용자 결정
                #   기존: role=CORE_MR + entry_type="MR" → DCA preorder 자동 등록, TP1 +1.5% limit
                #   변경: role=CORE_MR_HEDGE + entry_type="TREND" → V14.14처럼 단발 진입 + trail
                #     DCA 차단 (entry_type=TREND, V14.2)
                #     TP1 limit 차단 (CORE_MR_HEDGE, V14.17-hf2)
                #     Trail trigger +0.7% / retrace 0.4% (V14.16 청산)
                #     Hard SL -1.5% limit preorder (V14.15 NOSLOT_HSL)
                #   MR 슬롯 카운트 (count_slots role=CORE_MR)에서 빠짐 → MR 슬롯 한도 우회
                #   V14.14와 동일한 단발 진입 흐름
                "entry_type":       "TREND",
                "role":             "CORE_MR_HEDGE",
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

        # ★ V14 [05-06]: HEDGE_COMP → TREND_COMP 회귀 (다른 sym 추세 방향, MR 풀사이즈)
        # 사용자 결정 [05-06]: "헷지 컴프말고 트랜드 컴프라니까 / 지금 1단으로 하던 mr을 트랜드 컴프한테 넘기고"
        # 메모리 [실측 V10.31u]: 이전 TREND_COMP -$30 순손실 (ARB -$50 등) 패턴 가능성 인지
        # V13에서 같은 sym 반대(HEDGE_COMP)였던 것을 다른 sym 추세 방향으로 변경
        # 사이즈는 V13 그대로 MR 풀사이즈 (T1+T2+T3 합산)

        # ★ V10.31AA: HEDGE_COMP_ENABLED flag 체크 (TREND_COMP도 같은 플래그 재사용)
        _hc_flag_ok = True
        try:
            from v9.config import HEDGE_COMP_ENABLED
            if not HEDGE_COMP_ENABLED:
                _hc_flag_ok = False
        except Exception: pass

        if _trend_signal_side and _hc_flag_ok:
            # ★ V14: 다른 sym 추세 방향 후보 선정 (NOSLOT 로직과 동일 universe 필터)
            _tc_opp_side = "sell" if trigger_side == "buy" else "buy"  # MR 반대방향
            _tc_held = {s for s, ss in st.items() if isinstance(ss, dict)
                        for _, p in iter_positions(ss)
                        if isinstance(p, dict) and float(p.get("amt", 0) or 0) > 0}
            _tc_entered = {i.symbol for i in intents}
            _tc_ohlcv_pool = snapshot.ohlcv_pool or {}
            _tc_prices = snapshot.all_prices or {}
            _tc_long_pool = set(getattr(snapshot, "global_targets_long", None) or [])
            _tc_short_pool = set(getattr(snapshot, "global_targets_short", None) or [])
            _tc_allowed_pool = _tc_long_pool if _tc_opp_side == "buy" else _tc_short_pool

            # ★ V14.1 [05-06]: TREND_COMP 슬롯 체크 — CORE_MR_HEDGE 별도 카운트
            #   기존 V14: _core_short/_core_long (V10.31u에서 CORE_MR + HEDGE 합계)
            #   변경 V14.1: CORE_MR_HEDGE는 이제 슬롯 분리됨 → 별도 카운트
            #   1대1 매칭 보호: TREND_COMP 슬롯이 MAX_MR_PER_SIDE 도달 시 skip
            #   (MR 4쌍 매칭 = TREND_COMP 4 한도)
            from v9.risk.slot_manager import count_slots as _cs_v141
            try:
                _hc_slots = _cs_v141(st, role_filter=None)  # 전체 카운트에서 HEDGE 별도 추출
                # 활성 CORE_MR_HEDGE 직접 카운트
                _hc_long_active = 0
                _hc_short_active = 0
                for _h_sym, _h_st in st.items():
                    if not isinstance(_h_st, dict):
                        continue
                    for _h_side, _h_p in iter_positions(_h_st):
                        if not isinstance(_h_p, dict):
                            continue
                        if float(_h_p.get("amt", 0) or 0) <= 0:
                            continue
                        if _h_p.get("role") != "CORE_MR_HEDGE":
                            continue
                        if _h_side == "buy":
                            _hc_long_active += 1
                        else:
                            _hc_short_active += 1
                _tc_opp_slots_count = _hc_short_active if _tc_opp_side == "sell" else _hc_long_active
            except Exception:
                _tc_opp_slots_count = 0
            
            if _tc_opp_slots_count >= MAX_MR_PER_SIDE:
                print(f"[TREND_COMP_SKIP] {symbol} → COMP {_tc_opp_side} HEDGE 슬롯풀({_tc_opp_slots_count}/{MAX_MR_PER_SIDE})")
            else:
                _tc_best_sym = None
                _tc_best_score = 0
                for _tc_sym in _tc_ohlcv_pool:
                    if _tc_sym == symbol or _tc_sym in _tc_held or _tc_sym in _tc_entered:
                        continue
                    if _tc_sym == "BTC/USDT":
                        continue
                    if _tc_sym not in _tc_allowed_pool:
                        continue
                    if _trend_cooldown.get(_tc_sym, 0) > now_ts:
                        continue
                    try:
                        from v9.strategy.symbol_stats import is_symbol_cooldown as _sc_tc
                        if _sc_tc(_tc_sym):
                            continue
                    except Exception:
                        pass
                    # ★ V10.31w: LONG_ONLY/SHORT_ONLY 심볼 필터
                    try:
                        from v9.config import LONG_ONLY_SYMBOLS as _LONG_ONLY_TC, SHORT_ONLY_SYMBOLS as _SHORT_ONLY_TC
                        if _tc_opp_side == "sell" and _tc_sym in _LONG_ONLY_TC:
                            continue
                        if _tc_opp_side == "buy" and _tc_sym in _SHORT_ONLY_TC:
                            continue
                    except Exception:
                        pass
                    _tc_corr = (getattr(snapshot, "correlations", None) or {}).get(_tc_sym, 0)
                    if _tc_corr < OPEN_CORR_MIN:
                        continue
                    _tc_pool = _tc_ohlcv_pool.get(_tc_sym, {})
                    _tc_15m = _tc_pool.get("15m", [])
                    if len(_tc_15m) < 35:
                        continue
                    _tc_cp = float(_tc_prices.get(_tc_sym, 0))
                    if _tc_cp <= 0:
                        continue
                    _tc_score = _calc_trend_score(_tc_15m)
                    # ★ V14.5 [05-06]: _TC_MIN 절대 임계 폐기 — 사용자 결정 "상대 평가"
                    #   NOSLOT과 동일 변경 (line 1043 참조)
                    if abs(_tc_score) > TREND_MAX_SCORE:
                        continue
                    if _tc_opp_side == "sell" and _tc_score < 0:
                        if abs(_tc_score) > _tc_best_score:
                            _tc_best_score = abs(_tc_score)
                            _tc_best_sym = _tc_sym
                    elif _tc_opp_side == "buy" and _tc_score > 0:
                        if _tc_score > _tc_best_score:
                            _tc_best_score = _tc_score
                            _tc_best_sym = _tc_sym

                if _tc_best_sym is not None:
                    _tc_curr_p = float(_tc_prices.get(_tc_best_sym, 0))
                    if _tc_curr_p > 0:
                        # ★ V14: TREND COMP MR 풀사이즈 (T1+T2+T3 합산 100%)
                        _tc_grid = total_cap / GRID_DIVISOR * LEVERAGE
                        _tc_notional = _tc_grid  # 풀사이즈 (1단 진입)
                        _tc_qty = _tc_notional / _tc_curr_p if _tc_notional >= 10 else 0
                        if _tc_qty > 0:
                            # TREND COMP는 1단 진입 → DCA pre-order 0
                            _tc_dca_targets = []
                            system_state["_pending_trend_comp"] = {
                                "symbol": _tc_best_sym,   # ★ V14: 다른 sym
                                "side": _tc_opp_side,
                                "qty": _tc_qty,
                                "price": _tc_curr_p,
                                "mr_symbol": symbol,
                                "dca_targets": _tc_dca_targets,
                                "regime": _btc_regime,
                                "ts": time.time(),
                            }
                            print(f"[TREND_COMP] 📊 {_tc_best_sym} {_tc_opp_side} "
                                  f"score={_tc_best_score:.2f} notional=${_tc_notional:.0f} "
                                  f"(MR {symbol} {trigger_side} 반대 추세)")
                            try:
                                from v9.logging.logger_csv import log_system
                                log_system("TREND_COMP", f"{_tc_best_sym} {_tc_opp_side} "
                                           f"notional={_tc_notional:.0f} score={_tc_best_score:.2f} ← MR {symbol} {trigger_side}")
                            except Exception: pass
                else:
                    print(f"[TREND_COMP_SKIP] {symbol} {trigger_side} → 반대 추세 sym 없음")
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
    # ★ V10.31AA: TREND_NOSLOT_ENABLED flag 체크 (MR 단일 모드 시 비활성)
    if _noslot_best:
        try:
            from v9.config import TREND_NOSLOT_ENABLED
            if not TREND_NOSLOT_ENABLED:
                _noslot_best = None
        except Exception:
            pass
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
        # ★ V14.4 [05-06]: score 1.0~2.0 차단 분기 제거 — 사용자 결정
        #   기존 V10.31b: 1.0~2.0 영역 "애매한 추세"로 차단
        #   변경: 검증 안 된 직관 기반 가드, 0.5~1.0은 OK인데 1.0~2.0은 차단의 비단조성 의심
        #   1주 운영 후 이 영역 진입 EV 데이터로 결정
        # 차단 분기 폐기 — _noslot_best 그대로 유지
    if _noslot_best:
        _ns = _noslot_best
        # ★ V10.31w: LONG_ONLY/SHORT_ONLY 심볼 보호 — universe 풀 필터 이후에도
        # ohlcv_pool/sticky 영향으로 전용 심볼 진입 가능성 실측 (FIL buy, XRP sell 위반 실제 발생)
        # universe pool 체크 외에 최종 발사 직전 방어선
        try:
            from v9.config import LONG_ONLY_SYMBOLS as _LONG_ONLY_NS, SHORT_ONLY_SYMBOLS as _SHORT_ONLY_NS
            _ns_violate = False
            if _ns["side"] == "sell" and _ns["sym"] in _LONG_ONLY_NS:
                _ns_violate = True
                _ns_why = "LONG_ONLY"
            elif _ns["side"] == "buy" and _ns["sym"] in _SHORT_ONLY_NS:
                _ns_violate = True
                _ns_why = "SHORT_ONLY"
            if _ns_violate:
                print(f"[NOSLOT_WHITELIST_BLOCK] {_ns['sym']} {_ns['side']} ({_ns_why} 심볼 — 전용 제약)")
                try:
                    from v9.logging.logger_csv import log_system
                    log_system("NOSLOT_WHITELIST_BLOCK",
                               f"{_ns['sym']} {_ns['side']} sig={_ns['sig_sym']} ({_ns_why})")
                except Exception: pass
                _noslot_best = None
        except Exception:
            pass
    if _noslot_best:
        _ns = _noslot_best
        _ns_prices = snapshot.all_prices or {}
        _ns_cp = float(_ns_prices.get(_ns["sym"], 0))
        if _ns_cp > 0:
            # ★ V14.2 [05-06]: NOSLOT 풀사이즈 1단 (TREND_COMP와 동일)
            # 사용자 결정: "노슬랏도 트랜드 컴프랑 동일하게 t1로만 진행"
            # 기존: T1 사이즈 (DCA_WEIGHTS[0] / sum) + DCA targets
            # 변경: 풀사이즈 (_ns_grid 통째) + DCA targets 빈 리스트 (entry_type=TREND가 _place_dca_preorders 차단)
            _ns_total_cap = _mr_available_balance(snapshot, st)  # ★ V10.31b: BC 차감
            _ns_grid = _ns_total_cap / GRID_DIVISOR * LEVERAGE
            
            # ★ V14.13 [05-06]: 후보 선정 시 결정된 size_mult 사용 (FULL/HALF)
            #   슬롯풀+ROI 조건에서 -1%~-2%면 0.5, ≤-2%면 1.0
            _ns13_mult = _ns.get("size_mult", 0.5)
            _ns_notional = _ns_grid * _ns13_mult
            _ns_size_tag = "FULL" if _ns13_mult >= 1.0 else "HALF"
            
            # ★ V14.15-hf2 [05-12]: V14.11 70% 가드 폐기 — MR 폐기(V14.14)와 충돌
            #   기존: NOSLOT 노출 ≤ MR 노출 × 70% (MR이 baseline)
            #   문제: V14.14 MR 폐기 → MR 활성 0 → baseline 0 → 모든 NOSLOT 차단 → 진입 0건
            #   해결: 사용자 결정 "자본 100% 슬랏 100%" 일관성 위해 가드 제거
            #   자본 한도는 잔고 × LEV로 거래소가 자동 제한 (마진 부족 시 reject)
            # (V14.11 가드 코드 전체 폐기)
            if _noslot_best:
                _ns_qty = _ns_notional / _ns_cp if _ns_notional >= 10 else 0
            else:
                _ns_qty = 0
            if _ns_qty > 0:
                _ns_dca = []  # ★ V14.2: 1단 진입, DCA targets 비활성
                _trend_cooldown[_ns["sym"]] = time.time() + TREND_COOLDOWN_SEC
                intents.append(Intent(
                    trace_id=_tid(),
                    intent_type=IntentType.OPEN,
                    symbol=_ns["sym"],
                    side=_ns["side"],
                    qty=_ns_qty,
                    price=None,
                    reason=f"TREND_NOSLOT_{_ns_size_tag}(score={_ns['score']:.1f},dir={_ns['side']},exp_roi={_ns.get('exposure_roi',0):.2f})",
                    metadata={
                        "atr": 0.0, "dca_targets": _ns_dca,
                        # ★ V14.2: role=CORE_MR_HEDGE (TREND_COMP와 동일 — 슬롯 분리, DCA 차단)
                        "role": "CORE_MR_HEDGE", "entry_type": "TREND",
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
        # ★ V11 [05-04]: 레짐 무관 TP1 limit 단일 모드 (사용자 결정)
        #   사용자 통찰 [05-04]: "트레일 없이 TP만 진행 레짐 상관없이"
        #   배경: 이전 V10에서는 HIGH 레짐 시 trail 모드 (max-gap 0.3% 추적)
        #         → +1.5% 도달 후 +1.8% 갔다가 +1.5% 떨어지면 +1.5% 청산
        #         → 그러나 가속 변동 시 +1.5% 도달 후 즉시 +1.2% 청산도 가능
        #         → 손익비 변동, 단순함 깨짐, V11 의도 위배
        #   V11: 모든 레짐에서 limit preorder 사용 (정확 +1.5% 체결)
        #   _manage_tp1_preorders가 처리, trail 코드 비활성
        if p.get("trim_trail_active"):
            p["trim_trail_active"] = False
            p["trim_trail_max"] = 0.0
        continue
        
        # ── 이하 HIGH 레짐 trail 코드 (V11에서 비활성, 보존) ──
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

        # ★ V10.31AM3 hotfix-15: TP1 = 무조건 전량 청산 (사용자 결정 [04-28])
        #   배경: hf-14까지 잔량 방어 4단 누적했으나 OP/ETH dust 계속 발생 → 코드 복잡도 ↑
        #   사용자: "T1 매도만 전량 하면 되잖아" — TP1 컨셉 명확화
        #   결정: TP1 발동 시 거래소 보유분 100% 청산. 잔량 발생 자체 차단.
        #   부수 효과: DCA 진행 시 T2/T3 추가분도 같이 청산됨 — TRIM은 별도 (T2/T3 trim 그대로)
        total_qty = float(p.get("amt", 0.0))
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

        # ★ V14.2 [05-06]: trail 폐기 — 사용자 결정 "레짐과 상관없이 trail 없이 trim 만"
        # 기존 V10.31g: HIGH 레짐 + T2만 trail, T3은 LIMIT 선주문 (_place_trim_preorders)
        # 변경 V14.2: 모든 레짐/tier에서 trail 비활성, _place_trim_preorders가 단일 청산 경로
        # 이전 trail 활성 잔존 정리 (재기동 시 자동 정리)
        if p.get("trim_trail_active"):
            p["trim_trail_active"] = False
            p["trim_trail_max"] = 0.0
        continue
        # ── 아래 trail 코드는 V14.2부터 진입 안 함 (위 continue로 차단) ──

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
        trim_qty = calc_trim_qty(amt, dca_level, ep=_ep, bal=_bal, mark_price=curr_p, t1_amt=float(p.get("t1_amt", 0) or 0), t2_amt=float(p.get("t2_amt", 0) or 0), t3_amt=float(p.get("t3_amt", 0) or 0))
        _min_qty = SYM_MIN_QTY.get(symbol, SYM_MIN_QTY_DEFAULT)
        if trim_qty < _min_qty:
            continue
        # ★ V10.31AM3 hotfix-14: TRIM 잔량 정밀도 방어 (TP1과 동일 패턴)
        #   잔량이 min_qty 1.5배 이내면 전량 청산 (float 정밀도 잔여 차단)
        if 0 < (amt - trim_qty) < _min_qty * 1.5:
            trim_qty = amt
        # ★ V10.31AM3 hotfix-14: 잔량 노셔널 $5 미달이면 전량 청산
        _remaining_notional_trim = (amt - trim_qty) * curr_p
        if 0 < _remaining_notional_trim < 5.0:
            trim_qty = amt
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
# ★ V10.31AO [04-30]: T2 다단계 디펜스 (T3 제거된 스켈핑 패러다임)
# ═════════════════════════════════════════════════════════════════
def plan_t2_defense_v2(snapshot: MarketSnapshot, st: Dict,
                        system_state: Dict = None,
                        exclude_syms: set = None) -> List[Intent]:
    """T2 다단계 디펜스 — 사다리 임계 기반 TRIM/SL/HARD_SL.

    사용자 컨셉 [V10.31AO 04-30]: T3 제거 + T2 단계에서 사다리식 보호.
    "맞으면 길게 가져가고 아니면 빠르게 컷" 직관 정합.

    사다리 (config.T2_DEFENSE_LADDER):
        worst -1.5% → ROI ≥ +0.5% TRIM (T2 사이즈 부분 청산, T2→T1 복귀)
        worst -2.0% → ROI ≥ -0.5% SL (회복 cut)
        worst -2.5% → ROI ≥ -1.5% SL (회복 cut)
        worst -3.0%               HARD_SL (즉시 전량, 무한 보유 차단)

    PTP 차단 정책: _ptp_active_syms 활성 심볼 → 본 함수 차단

    중복 발동 방지: 포지션별 _t2_def_v2_last_step (worst_enter 값) 추적
    """
    intents: List[Intent] = []
    if exclude_syms is None:
        exclude_syms = set()

    _ptp_active = set((system_state or {}).get("_ptp_active_syms", set()) or set())

    for symbol, sym_st in st.items():
        if not isinstance(sym_st, dict):
            continue
        if symbol in exclude_syms or symbol in _ptp_active:
            continue
        for _iter_side, p in iter_positions(sym_st):
            if not isinstance(p, dict):
                continue
            # ★ V14 [05-06]: dca_level + role 분기 + 사다리 변수 동적 선택
            #   - V11 모드: MR T1 사다리 (T1_HEDGE_LADDER 사용 — V11 사양)
            #   - V13/V14 모드:
            #       role=CORE_MR_HEDGE + dca_level=1 → T1_HEDGE_LADDER (HEDGE/TREND_COMP)
            #       role=CORE_MR + dca_level=2 → T2_DEFENSE_LADDER (V14 신규: -2.5/0.5 TRIM)
            #       그 외 → continue (T3는 plan_t3_defense_v2 별도 처리)
            from v9.config import DCA_WEIGHTS as _V11_DCA_W
            from v9.config import T1_HEDGE_LADDER, calc_t1_hedge_action
            _is_v11 = (len(_V11_DCA_W) == 1 and _V11_DCA_W[0] == 100)
            
            _role_pdef = p.get("role", "")
            _dca_lv_pdef = int(p.get("dca_level", 1) or 1)
            
            # ladder 변수 + 매칭 함수 동적 선택
            if _is_v11:
                # V11 모드 (MR T1 사다리)
                if _role_pdef != "CORE_MR" or _dca_lv_pdef != 1:
                    continue
                _active_ladder = T1_HEDGE_LADDER
                _calc_action = calc_t1_hedge_action
                _step_key = "_t1_hedge_last_step"
            elif _role_pdef == "CORE_MR_HEDGE" and _dca_lv_pdef == 1:
                # ★ V14.10 [05-06]: NOSLOT 청산 직접 처리 (사다리 폐기) — 사용자 데이터 분석 결과
                #   기존 V14.7: T1_HEDGE_LADDER (-1.5/-1.7/-1.9) 사다리
                #   변경: trail + hard SL 조합 (옵션 3, 데이터 시뮬 13× 개선)
                #   alpha: max ≥ +1.0% 도달 시 trail 활성, max - 0.3% 회귀 시 익절
                #          그 외엔 worst ≤ -1.0% 도달 시 hard SL
                _amt_ns = float(p.get("amt", 0) or 0)
                if _amt_ns <= 0:
                    continue
                _ep_ns = float(p.get("ep", 0) or 0)
                _curr_p_ns = float((snapshot.all_prices or {}).get(symbol, 0) or 0)
                if _ep_ns <= 0 or _curr_p_ns <= 0:
                    continue
                _side_ns = p.get("side", "")
                _is_long_ns = (_side_ns == "buy")
                
                # ROI 계산
                if _is_long_ns:
                    _roi_ns = (_curr_p_ns - _ep_ns) / _ep_ns * 100 * LEVERAGE
                else:
                    _roi_ns = (_ep_ns - _curr_p_ns) / _ep_ns * 100 * LEVERAGE
                
                # max/worst 추적
                _max_ns = float(p.get("max_roi", 0) or 0)
                _worst_ns = float(p.get("worst_roi", 0) or 0)
                if _roi_ns > _max_ns:
                    p["max_roi"] = _roi_ns
                    _max_ns = _roi_ns
                if _roi_ns < _worst_ns:
                    p["worst_roi"] = _roi_ns
                    _worst_ns = _roi_ns
                
                # trail 활성 체크 (max ≥ +0.5% 처음 도달 시) ★ V14.14: 1.0 → 0.5
                _trail_active_ns = bool(p.get("noslot_trail_active", False))
                if not _trail_active_ns and _max_ns >= 0.7:  # ★ V14.16: 0.5 → 0.7
                    p["noslot_trail_active"] = True
                    p["noslot_trail_max"] = _max_ns
                    _trail_active_ns = True
                    print(f"[NOSLOT_TRAIL_ARM] {symbol} {_side_ns} max={_max_ns:.2f}% trail 활성")
                
                # 청산 결정
                _fire_ns = False
                _reason_ns = ""
                if _trail_active_ns:
                    # trail max 갱신 (= max_roi와 동기화)
                    _trail_max_ns = float(p.get("noslot_trail_max", 0) or 0)
                    if _max_ns > _trail_max_ns:
                        p["noslot_trail_max"] = _max_ns
                        _trail_max_ns = _max_ns
                    # retrace 0.3% 도달 시 cut
                    if _roi_ns <= _trail_max_ns - 0.4:  # ★ V14.16: 0.3 → 0.4
                        _fire_ns = True
                        _reason_ns = f"NOSLOT_TRAIL(peak={_trail_max_ns:.2f},roi={_roi_ns:.2f})"
                else:
                    # hard SL -1.5%  ★ V14.14: -1.0 → -1.5 (TREND COUNTER 데이터 최적)
                    if _worst_ns <= -1.5:
                        _fire_ns = True
                        _reason_ns = f"NOSLOT_HARD_SL(worst={_worst_ns:.2f},roi={_roi_ns:.2f})"
                
                if not _fire_ns:
                    continue
                
                # 시장가 cut 발사
                # ★ V14.15: hsl_preorder 등록된 경우 cancel 신호 (runner가 처리)
                _hsl_oid = (p.get("noslot_hsl_preorder", {}) or {}).get("oid", "")
                intents.append(Intent(
                    trace_id=_tid(),
                    intent_type=IntentType.TP1,
                    symbol=symbol,
                    side="sell" if _is_long_ns else "buy",
                    qty=_amt_ns,
                    price=None,
                    reason=_reason_ns,
                    metadata={
                        "force_market": True,
                        "is_force_close": True,
                        "_expected_role": "CORE_MR_HEDGE",
                        "cancel_noslot_hsl_oid": _hsl_oid,  # ★ V14.15: cancel 신호
                    },
                ))
                try:
                    from v9.logging.logger_csv import log_system
                    log_system("NOSLOT_CUT", f"{symbol} {_side_ns} {_reason_ns}")
                except Exception: pass
                continue  # 다음 포지션
            elif _role_pdef == "CORE_MR" and _dca_lv_pdef == 2:
                # V14: MR T2 trim 사다리
                _active_ladder = T2_DEFENSE_LADDER
                _calc_action = calc_t2_defense_action
                _step_key = "_t2_def_v2_last_step"
            else:
                continue
            _role = _role_pdef
            _amt = float(p.get("amt", 0) or 0)
            if _amt <= 0:
                continue
            ep = float(p.get("ep", 0) or 0)
            if ep <= 0:
                continue
            curr_p = float((snapshot.all_prices or {}).get(symbol, 0) or 0)
            if curr_p <= 0:
                continue

            side = p.get("side", "")
            roi = calc_roi_pct(ep, curr_p, side, LEVERAGE)
            worst = float(p.get("worst_roi", 0) or 0)
            max_r = float(p.get("max_roi_seen", 0) or 0)

            # 사다리 액션 결정 (동적 선택)
            action = _calc_action(worst, max_r)
            if action is None:
                continue
            mode, exit_roi = action

            # 중복 발동 방지: 같은 단계는 한 번만 (_step_key 동적)
            _last_step = float(p.get(_step_key, 0) or 0)
            _curr_step_worst = None
            for w_enter, _ex, _md in _active_ladder:
                if worst <= w_enter:
                    _curr_step_worst = w_enter
                else:
                    break
            if _curr_step_worst is None:
                continue
            if _last_step <= _curr_step_worst:
                if mode != "HARD_SL":
                    continue

            # 발동 조건 체크
            _fire = False
            _reason = ""
            if mode == "HARD_SL":
                _fire = True
                _reason = f"T2_DEF_HARD_SL(worst={worst:.2f}%)"
            elif mode == "SL":
                if roi >= exit_roi:
                    _fire = True
                    _reason = f"T2_DEF_SL(worst={worst:.2f}%,exit={exit_roi:+.1f}%,roi={roi:+.1f}%)"
            elif mode == "TRIM":
                if roi >= exit_roi:
                    _fire = True
                    _reason = f"T2_DEF_TRIM(worst={worst:.2f}%,exit={exit_roi:+.1f}%,roi={roi:+.1f}%)"

            if not _fire:
                continue

            p[_step_key] = _curr_step_worst

            _bal = float(getattr(snapshot, "real_balance_usdt", 0) or 0)
            _min_qty = SYM_MIN_QTY.get(symbol, SYM_MIN_QTY_DEFAULT)

            if mode in ("SL", "HARD_SL"):
                # 전량 컷
                _qty = _amt
                if _qty < _min_qty:
                    continue
                # ★ V10.31AO: HARD_SL 쿨다운용 history 기록 (system_state)
                if system_state is not None:
                    _hsl_hist = system_state.setdefault("_hard_sl_history", [])
                    _hsl_hist.append({
                        "ts": time.time(),
                        "side": side,
                        "sym": symbol,
                        "reason": _reason,
                    })
                intents.append(Intent(
                    trace_id=_tid(),
                    intent_type=IntentType.CLOSE,
                    symbol=symbol,
                    side="sell" if side == "buy" else "buy",
                    qty=_qty,
                    price=curr_p,
                    reason=_reason,
                    metadata={
                        "force_market": True,
                        "is_force_close": True,
                        "t2_defense_v2": True,
                        "t2_def_mode": mode,
                        "t2_def_step_worst": _curr_step_worst,
                        "snap_ts": getattr(snapshot, "ts", 0),
                    },
                ))
                # ★ V11 hf8 [05-05]: cancel queue 등록 제거 — 1분 reconcile이 처리
                #   기존 V11 hf2 [05-05]: 사다리 force_market 발사 시점에 SL cancel queue 등록
                #   변경: cancel 책임 일원화 — _tick_register_stop_sl 1분 reconcile
                #         force_market 청산 후 1분 안에 거래소 fetch 기준으로 cancel
                print(f"[T2_DEF_V2] ⛔ {symbol} {side} {mode} qty={_qty} roi={roi:+.2f}% worst={worst:+.2f}%")
                # ★ V10.31AO-hf10 [05-02]: 사다리 cut ml 기록
                try:
                    from v9.logging.logger_ml import record_ml_event as _rec_ml_def
                    from v9.config import LEVERAGE as _LEV_DEF
                    _rec_ml_def(
                        trace_id=intents[-1].trace_id,
                        event_type=f"T2_DEF_{mode}",
                        p=p, sym=symbol, snapshot=snapshot, st=st,
                        real_balance=float(getattr(snapshot, 'real_balance_usdt', 0) or 0),
                        leverage=_LEV_DEF, log_dir="v9_logs",
                    )
                except Exception:
                    pass
            else:  # TRIM
                # ★ V10.31AO: T2 사이즈만 부분 청산 (T2→T1 복귀)
                _trim_qty = calc_trim_qty(_amt, 2, ep=ep, bal=_bal, mark_price=curr_p, t1_amt=float(p.get("t1_amt", 0) or 0), t2_amt=float(p.get("t2_amt", 0) or 0), t3_amt=float(p.get("t3_amt", 0) or 0))
                if _trim_qty < _min_qty:
                    continue
                if 0 < (_amt - _trim_qty) < _min_qty * 1.5:
                    _trim_qty = _amt
                _remaining_notional_t2 = (_amt - _trim_qty) * curr_p
                if 0 < _remaining_notional_t2 < 5.0:
                    _trim_qty = _amt
                intents.append(Intent(
                    trace_id=_tid(),
                    intent_type=IntentType.TP1,
                    symbol=symbol,
                    side="sell" if side == "buy" else "buy",
                    qty=_trim_qty,
                    price=curr_p,
                    reason=_reason,
                    metadata={
                        "force_market": True,
                        "is_trim": True,
                        "target_tier": 1,  # ★ V10.31AO: T2 → T1 복귀
                        "t2_defense_v2": True,
                        "t2_def_mode": mode,
                        "t2_def_step_worst": _curr_step_worst,
                        "roi_gross": roi,
                        "snap_ts": getattr(snapshot, "ts", 0),
                    },
                ))
                print(f"[T2_DEF_V2] ✂ {symbol} {side} TRIM qty={_trim_qty} roi={roi:+.2f}% worst={worst:+.2f}% (T2→T1)")
                # ★ V10.31AO-hf10 [05-02]: TRIM cut ml 기록
                try:
                    from v9.logging.logger_ml import record_ml_event as _rec_ml_trim
                    from v9.config import LEVERAGE as _LEV_TRIM
                    _rec_ml_trim(
                        trace_id=intents[-1].trace_id,
                        event_type=f"T2_DEF_{mode}",
                        p=p, sym=symbol, snapshot=snapshot, st=st,
                        real_balance=float(getattr(snapshot, 'real_balance_usdt', 0) or 0),
                        leverage=_LEV_TRIM, log_dir="v9_logs",
                    )
                except Exception:
                    pass

    return intents


# ★ V13 [05-06]: T3 다단계 디펜스 부활 (사용자 사양 사다리)
def plan_t3_defense_v2(snapshot: MarketSnapshot, st: Dict,
                        system_state: Dict = None,
                        exclude_syms: set = None) -> List[Intent]:
    """T3 다단계 디펜스 — V13 사용자 사양 사다리.
    
    사다리 (config.T3_DEFENSE_LADDER):
        worst -4.0% → ROI ≥ -2.5% SL (회복 cut)
        worst -4.5% → ROI ≥ -3.0% SL
        worst -5.0% → ROI ≥ -4.0% SL
        worst -5.5%             HARD_SL (즉시 전량 컷)
    
    PTP 차단 정책: _ptp_active_syms 활성 심볼 → 본 함수 차단
    중복 발동 방지: 포지션별 _t3_def_v2_last_step 추적
    """
    intents: List[Intent] = []
    if exclude_syms is None:
        exclude_syms = set()
    
    _ptp_active = set((system_state or {}).get("_ptp_active_syms", set()) or set())
    
    for symbol, sym_st in st.items():
        if not isinstance(sym_st, dict):
            continue
        if symbol in exclude_syms or symbol in _ptp_active:
            continue
        for _iter_side, p in iter_positions(sym_st):
            if not isinstance(p, dict):
                continue
            # T3만 처리 (dca_level=3)
            if int(p.get("dca_level", 1) or 1) != 3:
                continue
            _role = p.get("role", "")
            if _role not in ("CORE_MR", "CORE_MR_HEDGE"):
                continue
            _amt = float(p.get("amt", 0) or 0)
            if _amt <= 0:
                continue
            ep = float(p.get("ep", 0) or 0)
            if ep <= 0:
                continue
            curr_p = float((snapshot.all_prices or {}).get(symbol, 0) or 0)
            if curr_p <= 0:
                continue
            
            side = p.get("side", "")
            roi = calc_roi_pct(ep, curr_p, side, LEVERAGE)
            worst = float(p.get("worst_roi", 0) or 0)
            max_r = float(p.get("max_roi_seen", 0) or 0)
            
            action = calc_t3_defense_action(worst, max_r)
            if action is None:
                continue
            mode, exit_roi = action
            
            # 중복 발동 방지
            _last_step = float(p.get("_t3_def_v2_last_step", 0) or 0)
            _curr_step_worst = None
            for w_enter, _ex, _md in T3_DEFENSE_LADDER:
                if worst <= w_enter:
                    _curr_step_worst = w_enter
                else:
                    break
            if _curr_step_worst is None:
                continue
            if _last_step <= _curr_step_worst:
                if mode != "HARD_SL":
                    continue
            
            # 발동 조건
            _fire = False
            _reason = ""
            if mode == "HARD_SL":
                _fire = True
                _reason = f"T3_DEF_HARD_SL(worst={worst:.2f}%)"
            elif mode == "SL":
                if roi >= exit_roi:
                    _fire = True
                    _reason = f"T3_DEF_SL(worst={worst:.2f}%,exit={exit_roi:+.1f}%,roi={roi:+.1f}%)"
            
            if not _fire:
                continue
            
            p["_t3_def_v2_last_step"] = _curr_step_worst
            
            _min_qty = SYM_MIN_QTY.get(symbol, SYM_MIN_QTY_DEFAULT)
            _qty = _amt  # T3 사다리는 전량 컷만 (TRIM 단계 없음)
            if _qty < _min_qty:
                continue
            
            if system_state is not None:
                _hsl_hist = system_state.setdefault("_hard_sl_history", [])
                _hsl_hist.append({
                    "ts": time.time(),
                    "side": side,
                    "sym": symbol,
                    "reason": _reason,
                })
            
            intents.append(Intent(
                trace_id=_tid(),
                intent_type=IntentType.CLOSE,
                symbol=symbol,
                side="sell" if side == "buy" else "buy",
                qty=_qty,
                price=curr_p,
                reason=_reason,
                metadata={
                    "force_market": True,
                    "is_force_close": True,
                    "t3_defense_v2": True,
                    "t3_def_mode": mode,
                    "t3_def_step_worst": _curr_step_worst,
                    "snap_ts": getattr(snapshot, "ts", 0),
                },
            ))
            print(f"[T3_DEF_V2] ⛔ {symbol} {side} {mode} qty={_qty} roi={roi:+.2f}% worst={worst:+.2f}%")
            try:
                from v9.logging.logger_ml import record_ml_event as _rec_ml_t3
                from v9.config import LEVERAGE as _LEV_T3
                _rec_ml_t3(
                    trace_id=intents[-1].trace_id,
                    event_type=f"T3_DEF_{mode}",
                    p=p, sym=symbol, snapshot=snapshot, st=st,
                    real_balance=float(getattr(snapshot, 'real_balance_usdt', 0) or 0),
                    leverage=_LEV_T3, log_dir="v9_logs",
                )
            except Exception:
                pass
    
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
# V10.31AB: 임계를 6h~7h로 앞당김 (데이터 기반)
# ═════════════════════════════════════════════════════════════════
def plan_t3_8h_cut(snapshot: "MarketSnapshot", st: Dict,
                   system_state: Dict) -> List[Intent]:
    """★ V10.31f/AB/AC: T3 포지션 시간 초과 시 단계적 정리.

    V10.31AB 변경: 임계 7h~8h → 6h~7h 앞당김
    V10.31AC 변경: HEDGE(CORE_MR_HEDGE) 포함 (T3 FC 방어)

    실측 근거 (4일 MR 99건):
      0~6h 구간: 평균 +$0.8~1.5/건 승률 90%+ (정상 작동)
      6~7h 구간: +$2.06/건 승률 100% (정상 회귀)
      7~8h 구간: -$10.25/건 승률 50% (급전환)
      8h+ 구간: -$4.92/건 승률 50% (계속 손실)
    → 7h가 명확한 변곡점. 7h 시장가 완료로 손실 구간 진입 전 차단.

    HEDGE 포함 근거 (V10.31AC):
      HEDGE_SIM 15건 100% VIRTUAL_TP1 성공
      실측 5건: TP1 4건 +$9, T3 FC 1건 -$17.75 (NEAR)
      HEDGE T3 도달 시 7h 시간컷으로 NEAR 같은 극단 손실 방어

    대상:
      - entry_type=MR (기존 MR 포지션)
      - entry_type=TREND + role=CORE_MR_HEDGE (HEDGE_COMP 포지션)
    제외:
      - BC/CB (별도 전략)
      - SOFT_HEDGE/INSURANCE_SH (봇 내부 보험 구조)

    단계:
      6h00 (step 0): limit +0.50% 유리방향 배치 (이익권 자연 유지)
      6h20 (step 1): 이전 limit 취소, +0.35% 재배치
      6h40 (step 2): 이전 limit 취소, +0.20% 재배치
      7h00 (step 3): 시장가 강제 정리

    유리한 방향:
      롱(buy) 포지션 청산 = sell @ curr × (1 + premium)
      숏(sell) 포지션 청산 = buy @ curr × (1 - premium)
    """
    import time as _time

    intents: List[Intent] = []
    now_ts = _time.time()

    # 단계 경계 (초 단위)
    # ★ V10.31AB: 7h~8h → 6h~7h 앞당김
    # 근거 (실측 4일 MR hold time 분석):
    #   6~7h: +$2.06/건 승률 100% (정상 회귀 여지 있음)
    #   7~8h: -$10.25/건 승률 50% (급전환 구간)
    #   7h가 명확한 변곡점 → 7h 시장가 완료 필요
    # 단계 설계:
    #   6h00: limit +0.50% (이익권이면 체결 안 되고 자연 유지)
    #   6h20: limit +0.35% (타이트화)
    #   6h40: limit +0.20%
    #   7h00: 시장가 강제 (손실 구간 진입 전 확정)
    T_STEP0 = 6 * 3600              # 21600 (6h00) — V10.31AB
    T_STEP1 = 6 * 3600 + 20 * 60    # 22800 (6h20)
    T_STEP2 = 6 * 3600 + 40 * 60    # 24000 (6h40)
    T_STEP3 = 7 * 3600              # 25200 (7h00 시장가)

    for symbol, p in _pos_items(st):
        # ★ V10.31AC: HEDGE(CORE_MR_HEDGE) 컷 대상 포함
        # 근거: NEAR HEDGE T3 FC -$17.75 (04-23), HEDGE도 T3까지 가면 손실 큼
        # 제외 대상은 봇 자체 구조물만 (BC/CB/보험 헷지)
        _role = p.get("role")
        if _role in ("BC", "CB", "SOFT_HEDGE", "INSURANCE_SH"):
            continue

        # ★ V10.31AC: MR + HEDGE 모두 허용 (entry_type=TREND + role=CORE_MR_HEDGE 포함)
        # 기존 V10.31j: MR only (TREND는 plan_t3_3h_cut_trend가 담당했으나 TREND 비활성으로 불필요)
        _entry_type = str(p.get("entry_type", "MR"))
        _is_mr       = (_entry_type == "MR")
        _is_hedge    = (_entry_type == "TREND" and _role == "CORE_MR_HEDGE")
        if not (_is_mr or _is_hedge):
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


# ★ V10.31AL: AH 이후 dead code 제거
# V10.31AH에서 자정 세션 리셋 로직을 완전 제거하면서 다음 함수들이 호출되지 않음:
#   - _ptp_session_date_kst: KST 자정 경계 판정용 (AH 이후 세션 경계 없음)
#   - _load_today_balance_stats: 재시작 시 오늘 balance 복원 (AH 이후 persist로 자동)
# 롤백 필요 시 git log V10.31AH 이전 버전에서 복원 가능.
# PTP_SESSION_TZ_OFFSET_SEC config 상수도 함께 제거.


def _ptp_update_state(system_state: Dict, current_balance: float,
                      st: Dict, now_ts: float) -> bool:
    """PTP 상태 관리 — 모드별 트리거 판정.

    ★ V10.31AN: PTP_TRIGGER_MODE에 따라 분기
      - "peak_drop": 기존 V10.31k~AM3 로직 (잔고 peak 대비 drop 감지)
      - "defense_close": 트리거는 strategy_core.apply_order_results의 hook에서 외부 설정
                         이 함수는 trigger lifecycle만 관리 (cooldown / 활성 상태 체크)

    공통:
      - _ptp_session_start / _ptp_peak_balance 는 양 모드 모두 추적 (대시보드 표시용)
      - _ptp_trigger_ts 활성 시 양 모드 모두 True 반환 (step 진행)
      - _ptp_cooldown_until 양 모드 공통 (1h 재트리거 차단)

    Returns:
        True: PTP 활성 (plan_portfolio_tp 실행 필요)
        False: 미활성
    """
    from v9.config import PTP_TRIGGER_MODE

    # ★ V10.31AH: 자정 세션 리셋 로직 완전 제거 — 진짜 무한 트레일 활성
    # peak/start는 양 모드 공통으로 추적 (peak_drop 모드 트리거 판정용 + 대시보드 표시용)
    # 최초 기동 시 1회 초기화 (peak/start 키가 아예 없을 때만)
    if "_ptp_session_start" not in system_state or "_ptp_peak_balance" not in system_state:
        system_state["_ptp_session_start"] = current_balance
        system_state["_ptp_peak_balance"] = current_balance
        try:
            from v9.logging.logger_csv import log_system
            log_system("PTP_INIT",
                       f"start=${current_balance:.2f} peak=${current_balance:.2f} "
                       f"mode={PTP_TRIGGER_MODE} (V10.31AN)")
        except Exception:
            pass

    session_start = float(system_state.get("_ptp_session_start", current_balance) or current_balance)
    if session_start <= 0:
        return False

    peak = float(system_state.get("_ptp_peak_balance", current_balance) or current_balance)

    # 2) Peak 갱신 (양 모드 공통 — 표시용)
    if current_balance > peak:
        system_state["_ptp_peak_balance"] = current_balance
        peak = current_balance

    # 3) 이미 트리거 중이면 True 반환 (step 진행) — 양 모드 공통
    if system_state.get("_ptp_trigger_ts"):
        return True

    # 4) 쿨다운 체크 — 양 모드 공통
    from v9.config import PTP_COOLDOWN_SEC
    _cooldown_until = float(system_state.get("_ptp_cooldown_until", 0.0) or 0.0)
    if _cooldown_until > 0 and now_ts < _cooldown_until:
        return False

    # ── 모드별 분기 ──────────────────────────────────────────────
    if PTP_TRIGGER_MODE == "shadow_only":
        # ★ V11 hf6 [05-05]: shadow_only = PTP 실전 완전 비활성
        #   사용자 보고: UNI 12:13 PTP 발동 → -0.09% close (사다리 우회)
        #   원인: shadow_only 분기 코드 없어서 peak_drop 그대로 작동
        #   해결: 즉시 False 반환 (PTP intent 생성 X)
        return False
    
    if PTP_TRIGGER_MODE == "defense_close":
        # ★ V10.31AN defense_close: 트리거는 strategy_core hook에서 외부 설정
        # 이 함수는 cooldown/이미 활성 체크만 담당 → 미활성 + 쿨다운 만료 시 False 반환
        return False

    # ── peak_drop 모드 (V10.31k~AM3 기존 로직) ───────────────────
    from v9.config import PTP_PEAK_TRIG_PCT, PTP_AVG_TIER_GATE

    # 5) Peak arm: peak_gain ≥ PTP_PEAK_TRIG_PCT
    peak_gain_pct = (peak - session_start) / session_start * 100.0
    if peak_gain_pct < PTP_PEAK_TRIG_PCT:
        return False

    # 6) Drop 조건 (J: tiered)
    drop_pct = (peak - current_balance) / session_start * 100.0
    drop_thresh = _ptp_get_drop_thresh(peak_gain_pct)
    if drop_thresh is None or drop_pct < drop_thresh:
        return False

    # 7) Tier gate (K): avg_dca_level ≥ PTP_AVG_TIER_GATE
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
        return False

    # 8) 트리거 확정 (peak_drop 모드)
    system_state["_ptp_trigger_ts"] = now_ts
    system_state["_ptp_last_step"] = -1
    system_state["_ptp_cooldown_until"] = now_ts + PTP_COOLDOWN_SEC
    try:
        from v9.logging.logger_csv import log_system
        log_system("PTP_TRIGGER",
                   f"mode=peak_drop peak={peak_gain_pct:.2f}% drop={drop_pct:.2f}%p "
                   f"avg_tier={avg_tier:.2f} bal=${current_balance:.2f} "
                   f"pos={len(tiers)}")
        print(f"[PTP_TRIGGER] mode=peak_drop peak={peak_gain_pct:.2f}% drop={drop_pct:.2f}%p "
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
    
    # ★ V10.31AM3 hotfix-19: hotfix-12 롤백 — PTP 실청산 부활 (사용자 결정 [04-29])
    #   근거 [실측 시뮬 3건 04-28~29]:
    #     케이스 1 (04-28 15:35) drop -0.93% → 시뮬 손실 -$24
    #     케이스 2 (04-29 03:10) drop -0.80% → 시뮬 손실 -$41
    #     케이스 3 (04-29 04:10) drop -1.02% → 시뮬 회복 +$2
    #     합계: PTP +$65 우세 (시뮬 -$2 vs PTP +$63 회피)
    #   hf-12 도입 근거였던 "회복 가능 포지션 자해" (04-27) → 변동성 시기엔 반대로 손실 cap 가치
    #   PTP 가치는 시기 의존 — 표본 부족하지만 시뮬 데이터가 PTP 우세 명확
    #   수치 검증: drop 0.8% 임계는 3건 모두 발동 (0.7로 좁히면 자해 ↑, 1.0으로 넓히면 -$65 놓침)
    #   → drop 임계 0.8% 유지가 데이터상 최적. 다른 수치(cooldown/step/premium) 변경 근거 부족
    #   하단 V10.31AJ~AM3 정상 청산 로직 복원 (active_syms, step 0/1, log_btc_context)
    
    # ★ V10.31AJ: trigger 활성 진입 즉시 _ptp_active_syms 세팅 (step gap 방지)
    # ★ V10.31AO-hf13 [05-04]: TREND_FILTER 기반 — _ptp_trigger_side 같은 방향만 청산
    #   사용자 통찰 [05-04]: 추세 반대 cut 발생 시 같은 방향 보유 = 같은 위험
    #   _ptp_trigger_side 미세팅 시 모든 sym (이전 동작) — 호환성
    _trigger_side = system_state.get("_ptp_trigger_side", None)
    _active_sym_set = set()
    try:
        from v9.execution.position_book import iter_positions as _ip_ptp
        for _s, _ss in st.items():
            if not isinstance(_ss, dict):
                continue
            for _sd, _pp in _ip_ptp(_ss):
                if not isinstance(_pp, dict):
                    continue
                if float(_pp.get("amt", 0) or 0) <= 0:
                    continue
                _r = _pp.get("role", "")
                # BC/CB/INSURANCE/SOFT_HEDGE/CORE_HEDGE는 PTP 대상 아님 (청산 제외)
                if _r in ("BC", "CB", "INSURANCE_SH", "SOFT_HEDGE", "CORE_HEDGE"):
                    continue
                # ★ hf13: trigger_side 세팅된 경우 같은 방향만 청산
                if _trigger_side and _sd != _trigger_side:
                    continue  # 반대 방향 보유 = 추세 따름 = 안전 → 보유 유지
                _active_sym_set.add(_s)
    except Exception:
        pass
    system_state["_ptp_active_syms"] = _active_sym_set
    
    # ★ V10.31AM: 2-step 구조 — step 0 (limit 0.05%) 1분 → step 1 (시장가)
    # 기존 4-step × 5분 (15분 소요)은 실측상 limit 대부분 미체결 + 지연 중 추가 손실
    trigger_ts = float(system_state["_ptp_trigger_ts"])
    elapsed = now_ts - trigger_ts
    
    from v9.config import PTP_STEP_INTERVAL_SEC, PTP_PREMIUMS_BY_STEP
    
    T_STEP0 = PTP_STEP_INTERVAL_SEC          # 1min (AM: 60s)
    
    if elapsed < T_STEP0:
        cur_step = 0
        premium = PTP_PREMIUMS_BY_STEP.get(0, 0.0005)
    else:
        cur_step = 1  # ★ V10.31AM: 기존 step 3 역할 — 시장가
        premium = 0.0
    
    last_step = int(system_state.get("_ptp_last_step", -1))
    if cur_step <= last_step:
        return []
    
    # 이전 단계 PTP limit 취소 (step 1~3)
    # ★ V10.31z 긴급: PTP 발동 시 TRIM / DCA_PRE / TP1_PRE preorder 전부 취소
    # 근거: ReduceOnly -2022 대량 발생 (실측 17:08~17:12 TIA/SUI/ADA 수십 회)
    # 원인: PTP 즉시 arming(V10.31y) + 기존 TRIM preorder ReduceOnly 경쟁
    # 수정: PTP 첫 step에서 모든 기존 reduce preorder 강제 취소
    if cur_step == 0:
        try:
            from v9.execution.order_router import get_pending_limits
            from v9.strategy.strategy_core import _TRIM_CANCEL_QUEUE
            _cancelled_all = 0
            for _pl_oid, _pl_info in list(get_pending_limits().items()):
                # is_trim / is_tp_pre / is_dca_pre / is_t3_3h_limit / is_t3_8h_limit 모두 취소
                if (_pl_info.get("is_trim") or _pl_info.get("is_tp_pre") or
                    _pl_info.get("is_dca_pre") or _pl_info.get("is_t3_3h_limit") or
                    _pl_info.get("is_t3_8h_limit") or _pl_info.get("is_ptp_limit")):
                    _TRIM_CANCEL_QUEUE.append({
                        "sym": _pl_info.get("sym", ""), "oid": _pl_oid,
                    })
                    _cancelled_all += 1
            if _cancelled_all > 0:
                print(f"[PTP] step 0 진입 — 기존 preorder {_cancelled_all}건 전체 취소")
                try:
                    from v9.logging.logger_csv import log_system
                    log_system("PTP_CANCEL_ALL_PREORDERS",
                               f"step=0 cancelled={_cancelled_all}")
                except Exception: pass
        except Exception as _ce:
            print(f"[PTP] step 0 preorder 취소 실패(무시): {_ce}")
    elif cur_step >= 1:
        # 이전 PTP step limit만 취소 (TRIM/DCA는 step 0에서 이미 청소됨)
        try:
            from v9.execution.order_router import get_pending_limits
            from v9.strategy.strategy_core import _TRIM_CANCEL_QUEUE
            _cancelled = 0
            _has_pending_ptp = False
            for _pl_oid, _pl_info in list(get_pending_limits().items()):
                if _pl_info.get("is_ptp_limit"):
                    _TRIM_CANCEL_QUEUE.append({
                        "sym": _pl_info.get("sym", ""), "oid": _pl_oid,
                    })
                    _cancelled += 1
                    _has_pending_ptp = True
            if _cancelled > 0:
                print(f"[PTP] step {cur_step} 이전 PTP limit {_cancelled}건 취소 예약 — 다음 tick에 force_close")
            # ★ V10.31AM HOTFIX: cancel과 force_close가 같은 tick에서 발생하면
            # 거래소에 limit 살아있는 채 시장가 도달 → -2022 ReduceOnly Rejected (실측 ATOM 04-25 01:10:36)
            # 해결: cancel 발생한 tick은 intent 발사 skip — 다음 tick (5초 뒤)에 cancel 완료된 상태에서 force_close
            if _has_pending_ptp:
                # last_step만 갱신하지 말고, 다음 tick에 다시 cur_step==1로 들어와서 처리되도록
                # last_step은 그대로 두고 빈 intents 반환
                return []
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
        
        # ★ V10.31AM: 2-step 구조 — step 0 limit, step 1 시장가
        if cur_step < 1:
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
    
    # ★ V10.31AM: 최종 step 완료 (기존 step 3 → step 1) — 세션 리셋
    if cur_step == 1:
        system_state["_ptp_session_start"] = current_balance  # 새 시작점
        system_state["_ptp_peak_balance"] = current_balance
        system_state.pop("_ptp_trigger_ts", None)
        system_state.pop("_ptp_last_step", None)
        system_state.pop("_ptp_active_syms", None)  # ★ V10.31AJ: preorder 차단 해제
        system_state.pop("_ptp_trigger_side", None)  # ★ V10.31AO-hf13: TREND_FILTER 기반 정리
        # ★ V10.31AM3 hotfix-9: PTP 후 신규 진입 차단 (사용자 결정 [04-27])
        #   "이벤트 끝난 두시간 정도" — 1차/2차 추세 cover, 추세 진정 후 진입
        from v9.config import PTP_ENTRY_COOLDOWN_SEC as _PEC
        system_state["_ptp_entry_cooldown_until"] = now_ts + _PEC
        try:
            from v9.logging.logger_csv import log_system
            log_system("PTP_COMPLETE",
                       f"bal=${current_balance:.2f} positions_closed={len(intents)} "
                       f"entry_cooldown={_PEC}s")
            print(f"[PTP] 완료 — 세션 리셋 bal=${current_balance:.2f} entry lock {_PEC//60}분")
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

    # ★ V10.31AM3 hotfix-6: 스큐+잔고 시계열 로깅 (60s throttle, 사용자 가설 검증 인프라)
    #   사용자 [04-27]: "균형 맞다가 한쪽으로 쏠리면 한쪽 익절 후 진입 안되고 스큐 차이"
    #   → 4주 누적 후 스큐 변화와 잔고 drop 시간 상관 분석 → 스큐+PTP 결합 검토
    try:
        _skew_last_log = getattr(generate_all_intents, "_skew_last_log_ts", 0)
        if _snap_ts - _skew_last_log >= 60:  # 60초 throttle
            from v9.logging.logger_csv import log_skew as _log_skew
            from v9.engines.hedge_core import calc_skew as _calc_skew
            _bal = float(getattr(snapshot, "real_balance_usdt", 0) or 0)
            if _bal > 0:
                _skew_abs, _long_m, _short_m = _calc_skew(st, _bal)
                _skew_signed = _long_m - _short_m
                _long_cnt, _short_cnt = _count_active_by_side(st)
                # peak/drop은 system_state에서 (PTP가 관리)
                _peak = float((system_state or {}).get("_ptp_peak_balance", _bal) or _bal)
                if _peak <= 0: _peak = _bal
                _drop_pct = (_bal - _peak) / _peak * 100 if _peak > 0 else 0.0
                _ptp_armed = float((system_state or {}).get("_ptp_peak_balance", 0) or 0) > 0
                _log_skew(
                    trace_id=str(int(_snap_ts)),
                    skew=_skew_abs,
                    long_m=_long_m,
                    short_m=_short_m,
                    skew_signed=_skew_signed,
                    long_count=_long_cnt,
                    short_count=_short_cnt,
                    balance=_bal,
                    peak_balance=_peak,
                    drop_pct=_drop_pct,
                    ptp_armed=_ptp_armed,
                    urgency=_urg_log.get("urgency", 0.0),
                )
                generate_all_intents._skew_last_log_ts = _snap_ts
    except Exception as _e:
        # 로깅 실패는 트레이딩에 영향 없게 — 조용히 skip
        pass

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

    # ★ V10.31AB: T3_8H (MR only, 6h~7h로 앞당김) 재활성
    # 근거: 실측 6~7h +$2/건(이익권) / 7~8h -$10/건(급전환)
    # PTP는 portfolio drop 감지 — 단일 포지션 물림은 놓칠 수 있음
    # 04-23 OP 7.2h -$14 케이스: 다른 MR +$16 상쇄로 PTP 미발동 → T3_8H가 방어
    # 주의: 함수명은 plan_t3_8h_cut 유지(호환성) but 내부 임계는 6h~7h (T_STEP0~3)
    # ★ V10.31AM3 hotfix-16: T3 8H 시간컷 폐지 (사용자 결정 [04-28])
    #   배경: T3 도달 후 worst 깊어지면 hf-4 T3 사다리(-2~-4.5%)가 손절 담당.
    #     8H 타임아웃은 사다리 미발동 + worst 얕은 채 장기 보유 케이스에서 작동.
    #     실측 [04-28 ETH] worst -1.45%, ROI -1.04% 10.5h 보유 후 8H 청산 -$4.24.
    #   사용자: "8시간 타임컷도 없애. 디펜스 모드 있으니 불필요"
    #     → T3 사다리(hf-4) + HARD_SL_BY_TIER(T3 -10%)로 단일 보호 충분.
    #     → 시간 기반 cap 제거, ROI/worst 기반만 유지 (컨셉 정합).
    #   부수 효과: T3 무한 보유 가능 (HARD_SL -10%까지). 슬롯 자본 묶임 vs 회복 기회 trade-off.
    # _t3_8h_intents = plan_t3_8h_cut(snapshot, st, system_state)
    # intents += _t3_8h_intents
    _t3_8h_intents = []  # 비활성
    _t3_8h_syms = set()

    # ★ V10.31y/AA: T3_3H 비활성 유지 — TREND 전용이었으나 TREND 자체 비활성
    # TREND_NOSLOT_ENABLED=False 상태라 T3_3H 대상 포지션 발생 안 함
    # _t3_3h_intents = plan_t3_3h_cut_trend(snapshot, st, system_state)
    # intents += _t3_3h_intents
    # _t3_3h_syms = {i.symbol for i in _t3_3h_intents}
    _t3_3h_syms = set()

    _fc_intents = plan_force_close(snapshot, st, system_state, _bad_regime_active)
    intents += _fc_intents
    _fc_syms = {i.symbol for i in _fc_intents}
    # ★ V10.31AJ: PTP trigger 활성 심볼은 trim/tp1에서 제외 (ReduceOnly -2022 방지)
    # 근거: PTP가 reduce limit 이미 배치한 상태에서 trim/tp1이 또 reduce 재시도 시
    #       거래소 reduce qty 합이 포지션 초과 → -2022 대량 발생 (실측 04-24 11:22+ INJ)
    # _ptp_active_syms는 plan_portfolio_tp가 step 발사 시 세팅, step3 완료 시 제거
    _ptp_active_syms = system_state.get("_ptp_active_syms", set()) or set()
    # T3 시간 컷 대상 심볼은 FC/TP1/TRIM 중복 방지 (빈 set이라 무해)
    _exclude = _fc_syms | _t3_8h_syms | _t3_3h_syms | _ptp_active_syms

    intents += plan_tp1(snapshot, st, exclude_syms=_exclude)
    intents += plan_trim_trail(snapshot, st, exclude_syms=_exclude)
    # ★ V10.31AO [04-30]: T2 다단계 디펜스 (T3 제거된 스켈핑 패러다임)
    #   사다리 -1.5/-2.0/-2.5/-3.0 단계별 TRIM/SL/HARD_SL
    #   PTP 활성 시 차단 (함수 내부에서 _ptp_active_syms 체크)
    intents += plan_t2_defense_v2(snapshot, st, system_state=system_state, exclude_syms=_exclude)
    # ★ V10.31AO: plan_t3_defense_v2는 stub (T3 제거) — 호출 유지하나 빈 리스트 반환
    intents += plan_t3_defense_v2(snapshot, st, system_state=system_state, exclude_syms=_exclude)
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
