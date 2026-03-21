"""
V10.10 Trinity — Hedge Engine (하드 헷지 + 레거시 호환)
================================================================
v10.9.1 → v10.10:
  - plan_soft_hedge 제거 (DCA_BLOCKED_INSURANCE로 대체 → planners.py)
  - apply_sh_open 제거 (strategy_core에서 직접 처리)
  - INSURANCE_SH role: plan_force_close에서 timecut 처리
  - HH: 기존 로직 유지 + v10.9.1 가드 유지

[Hard Hedge]
  - plan_hedge_exit()  → HH 종료 (소스T5컷, 소스부재, 소스TP1)
  - check_hedge_tp1()  → HH TP1 판단
  - apply_hedge_close() → 청산 시 rolling_count 증가
"""
import time
import uuid
from typing import List, Dict

from v9.types import Intent, IntentType, MarketSnapshot
from v9.risk.slot_manager import count_slots
from v9.execution.position_book import get_p, set_p, iter_positions, is_active
from v9.config import (
    LEVERAGE, TOTAL_MAX_SLOTS, MAX_LONG, MAX_SHORT,
    DCA_WEIGHTS, TP1_PCT, TP1_PCT_BY_DCA,
)
from v9.utils.utils_math import calc_roi_pct, calc_rsi, calc_ema, atr_from_ohlcv


def _tid() -> str:
    return str(uuid.uuid4())[:8]


# ═════════════════════════════════════════════════════════════════
# Hedge 종료 (plan_force_close에서 호출)
# ═════════════════════════════════════════════════════════════════
def plan_hedge_exit(
    symbol: str, p: dict, curr_p: float, roi_pct: float,
    st: dict, snapshot: MarketSnapshot, closing_set: set,
) -> tuple:
    force = False
    reason = ""
    extra_intents = []
    _h_role = p.get("role", "")

    if _h_role == "SOFT_HEDGE":
        pass  # 레거시: trailing이 처리

    elif _h_role == "HEDGE":
        src_sym  = p.get("source_sym", "")
        src_st   = st.get(src_sym, {}) if src_sym else {}
        src_side = "sell" if p.get("side") == "buy" else "buy"
        src_p    = get_p(src_st, src_side)
        hedge_roi = roi_pct

        if hedge_roi >= 2.0 and isinstance(src_p, dict):
            src_curr_p = float((snapshot.all_prices or {}).get(src_sym, 0.0))
            if src_curr_p > 0:
                _src_t5_ep = float(src_p.get("t5_entry_price", 0.0) or 0.0)
                src_roi = calc_roi_pct(
                    float(src_p.get("ep", 0.0)), src_curr_p, src_side, LEVERAGE)
                _src_cut = (
                    calc_roi_pct(_src_t5_ep, src_curr_p, src_side, LEVERAGE) <= -5.5
                    if _src_t5_ep > 0 else src_roi <= -5.0)
                if _src_cut:
                    src_is_long = src_side == "buy"
                    closing_set.add((src_sym, src_side))
                    extra_intents.append(Intent(
                        trace_id=_tid(), intent_type=IntentType.FORCE_CLOSE,
                        symbol=src_sym,
                        side="sell" if src_is_long else "buy",
                        qty=float(src_p.get("amt", 0.0)), price=src_curr_p,
                        reason=f"HEDGE_SRC_CUT(src={src_roi:.1f}%,hedge={hedge_roi:.1f}%)",
                        metadata={"roi_pct": src_roi, "paired_hedge": symbol},
                    ))
        elif not isinstance(src_p, dict):
            if hedge_roi > 0:
                if p.get("step", 0) < 1:
                    p["step"] = 1
                    p["tp1_done"] = True
                    p["trailing_on_time"] = time.time()
                    p["source_sl_orphan"] = True
                    print(f"[HEDGE] {symbol} SRC_GONE 수익({hedge_roi:+.1f}%) → trailing")
            else:
                force = True
                reason = f"HEDGE_SRC_GONE(hedge={hedge_roi:.1f}%)"
        elif isinstance(src_p, dict):
            src_curr_p = float((snapshot.all_prices or {}).get(src_sym, 0.0))
            if src_curr_p > 0:
                src_roi = calc_roi_pct(
                    float(src_p.get("ep", 0.0)), src_curr_p, src_side, LEVERAGE)
                _src_dca = int(src_p.get("dca_level", 1) or 1)
                src_tp1 = TP1_PCT_BY_DCA.get(_src_dca, TP1_PCT)
                if src_roi >= src_tp1:
                    force = True
                    reason = f"HEDGE_SRC_TP1(src={src_roi:.1f}%,hedge={hedge_roi:.1f}%)"

    return force, reason, extra_intents


# ═════════════════════════════════════════════════════════════════
# Hedge TP1 판단 (plan_tp1에서 호출)
# ═════════════════════════════════════════════════════════════════
def check_hedge_tp1(p: dict, curr_p: float) -> tuple:
    _h_ep = float(p.get("hedge_entry_price", 0.0) or 0.0)
    if _h_ep <= 0:
        _h_ep = float(p.get("ep", 0.0) or 0.0)
    roi_gross = calc_roi_pct(_h_ep, curr_p, p.get("side", ""), LEVERAGE)
    tp1_thresh = 0.5
    return roi_gross >= tp1_thresh, roi_gross, tp1_thresh


# ═════════════════════════════════════════════════════════════════
# 체결 처리 (strategy_core에서 호출)
# ═════════════════════════════════════════════════════════════════
def apply_hedge_close(p: dict, sym: str, avg_px: float, st: dict, snapshot, now: float) -> None:
    _role = p.get("role", "")

    if _role in ("SOFT_HEDGE", "INSURANCE_SH"):
        pass  # 소스 영향 0

    elif _role == "HEDGE":
        _src_sym  = p.get("source_sym", "") or sym
        _src_side = "sell" if p.get("side") == "buy" else "buy"
        _src_st   = st.get(_src_sym, {}) if _src_sym else {}
        _src_p    = get_p(_src_st, _src_side) if _src_st else None

        if isinstance(_src_p, dict):
            _src_role = _src_p.get("role", "")
            if _src_role in ("HEDGE", "SOFT_HEDGE", "INSURANCE_SH"):
                print(f"[HH_GUARD] {sym} 소스 lookup이 {_src_role}를 가리킴 — rolling 스킵")
                return
            _src_p_side = _src_p.get("side", "")
            if _src_p_side != _src_side:
                print(f"[HH_GUARD] {sym} 소스 side 불일치 — 스킵")
                return

            _src_p["hedge_rolling_count"] = _src_p.get("hedge_rolling_count", 0) + 1
            _exit_px = float(avg_px if avg_px > 0 else (snapshot.all_prices or {}).get(sym, 0.0))
            _src_p["last_hedge_exit_p"]    = _exit_px
            _src_p["last_hedge_exit_side"] = p.get("side", "")
