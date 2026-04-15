"""
★ V10.29e: plan_force_close — HARD_SL / ZOMBIE / T3방어 / DD_SHUTDOWN.
planners.py에서 분리. hedge_engine.py(데드파일) 재활용.
"""
import time
import uuid
from typing import List, Dict

from v9.types import Intent, IntentType, MarketSnapshot
from v9.execution.position_book import iter_positions, get_p
from v9.risk.slot_manager import count_slots
from v9.utils.utils_math import calc_roi_pct
from v9.config import (
    LEVERAGE, MAX_LONG, MAX_SHORT,
    SYM_MIN_QTY, SYM_MIN_QTY_DEFAULT,
    get_sl_entry,
)


# ═════════════════════════════════════════════════════════════════
# ZOMBIE 상수 + 모듈 상태
# ═════════════════════════════════════════════════════════════════
ZOMBIE_ROI_THRESH   = -5.0
ZOMBIE_COOLDOWN_SEC = 8 * 3600
ZOMBIE_BATCH_TP_ROI = {1: 4.0, 2: 2.0}
ZOMBIE_TIME_CUT_SEC = 12 * 3600
_zombie_cooldown = {"buy": 0.0, "sell": 0.0}


def _tid() -> str:
    return str(uuid.uuid4())[:8]


def _zombie_exit(p: dict, roi_pct: float, now: float,
                 bad_regime_active: bool = False,
                 atr_pct: float = 0.0, snapshot=None) -> tuple:
    """V10.17 ZOMBIE — BAD 레짐 + 슬롯풀 + T2+ + ROI≤-5%."""
    _role = p.get("role", "")
    if _role in ("HEDGE", "SOFT_HEDGE", "INSURANCE_SH", "CORE_HEDGE"):
        return False, ""
    dca_level = int(p.get("dca_level", 1) or 1)
    if dca_level < 2:
        return False, ""
    if not bad_regime_active:
        return False, ""
    if roi_pct > ZOMBIE_ROI_THRESH:
        return False, ""
    _side = p.get("side", "buy")
    if now < _zombie_cooldown.get(_side, 0.0):
        return False, ""
    return True, f"ZOMBIE_T{dca_level}_roi{roi_pct:.1f}%"


# ═════════════════════════════════════════════════════════════════
# plan_force_close
# ═════════════════════════════════════════════════════════════════
def plan_force_close(
    snapshot: MarketSnapshot,
    st: Dict,
    system_state: Dict,
    bad_regime_active: bool = False,
) -> List[Intent]:
    intents: List[Intent] = []
    shutdown_active = system_state.get("shutdown_active", False)
    now = time.time()
    _closing_set: set = set()

    for sym, sym_st in st.items():
        if not isinstance(sym_st, dict):
            continue
        for pos_side, p in iter_positions(sym_st):
            if not isinstance(p, dict):
                continue
            p["side"] = pos_side
            symbol = sym

            curr_p = float((snapshot.all_prices or {}).get(symbol, 0.0))
            if curr_p <= 0:
                continue

            is_long = p.get("side", "") == "buy"
            roi_pct = calc_roi_pct(p.get("ep", 0.0), curr_p, p.get("side", ""), LEVERAGE)

            force  = False
            reason = ""

            # 잔량 정리
            _res_amt = float(p.get("amt", 0.0) or 0.0)
            _res_notional = _res_amt * curr_p
            _res_min_qty = SYM_MIN_QTY.get(symbol, SYM_MIN_QTY_DEFAULT)
            # ★ V10.30 FIX: float epsilon(2.84e-14) 차단 + reduce_fail 쿨다운 존중
            # ★ V10.31b FIX: 필드명 통일 (runner는 exit_fail_cooldown_until 세팅)
            _res_cd = float(sym_st.get("exit_fail_cooldown_until", 0) or 0)
            if _res_amt > _res_min_qty * 0.01 and _res_cd < now:
                if _res_notional < 20.0 or _res_amt < _res_min_qty * 2:
                    force  = True
                    reason = f"RESIDUAL_CLEANUP(${_res_notional:.2f},qty={_res_amt})"

            # DD_SHUTDOWN
            if shutdown_active:
                _dd_role = p.get("role", "")
                _dd_step = int(p.get("step", 0) or 0)
                if _dd_role in ("HEDGE", "SOFT_HEDGE", "INSURANCE_SH"):
                    pass
                elif _dd_step >= 1:
                    pass
                else:
                    force  = True
                    reason = "DD_SHUTDOWN_FORCE_CLOSE"

            # HEDGE/SOFT_HEDGE → hedge_engine_v2 위임
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

            # INSURANCE_SH — BTC 반전 기반 청산
            elif p.get("role") == "INSURANCE_SH":
                _ins_time = float(p.get("time", now) or now)
                _ins_age = now - _ins_time

                from v9.config import (INSURANCE_TP_ROI, INSURANCE_CUT_ROI,
                                       INSURANCE_MAX_HOLD_SEC)

                btc_pool_ins = (snapshot.ohlcv_pool or {}).get("BTC/USDT", {})
                btc_1m_ins = btc_pool_ins.get("1m", [])
                _btc_reversed = False
                if len(btc_1m_ins) >= 2:
                    _btc_now_ins = float(btc_1m_ins[-1][4])
                    _btc_entry = float(p.get("hedge_entry_price", 0) or _btc_now_ins)
                    _ins_side = p.get("side", "")
                    if _ins_side == "sell" and _btc_now_ins > _btc_entry * 1.003:
                        _btc_reversed = True
                    elif _ins_side == "buy" and _btc_now_ins < _btc_entry * 0.997:
                        _btc_reversed = True

                if _ins_age >= 180 and _btc_reversed and roi_pct < 0:
                    force = True
                    reason = f"INSURANCE_SH_REVERSED({_ins_age:.0f}s,roi={roi_pct:+.1f}%)"
                elif roi_pct >= INSURANCE_TP_ROI:
                    p["step"] = 1
                    p["tp1_done"] = True
                    p["trailing_on_time"] = now
                    p["max_roi_seen"] = max(float(p.get("max_roi_seen", 0) or 0), roi_pct)
                    print(f"[INSURANCE_SH] {symbol} roi={roi_pct:+.1f}% → trailing")
                    continue
                elif _ins_age >= 600 and roi_pct < INSURANCE_CUT_ROI:
                    force = True
                    reason = f"INSURANCE_SH_TIMECUT(10m,roi={roi_pct:+.1f}%)"
                elif _ins_age >= 600 and roi_pct > 0:
                    p["step"] = 1
                    p["tp1_done"] = True
                    p["trailing_on_time"] = now
                    p["max_roi_seen"] = max(float(p.get("max_roi_seen", 0) or 0), roi_pct)
                    print(f"[INSURANCE_SH] {symbol} 10m roi={roi_pct:+.1f}% → trailing")
                    continue
                elif _ins_age >= INSURANCE_MAX_HOLD_SEC:
                    force = True
                    reason = f"INSURANCE_SH_MAXTIME(20m,roi={roi_pct:+.1f}%)"

            # BC/CB — 자체 SL/TP 사용
            elif p.get("role") in ("BC", "CB"):
                pass

            else:
                # ── HARD_SL (CORE 포지션 전용) ────────────────────────
                _dca_lv_sl = int(p.get("dca_level", 1) or 1)

                from v9.config import HARD_SL_BY_TIER

                _T4_ENTRY   = -7.0    # ★ V10.29e: -7% 터치 → TP -1% (공식: 2×-7+13=-1)
                _T4_HARD_SL = -12.0

                if _dca_lv_sl >= 3:
                    _sl_ep = get_sl_entry(p, _dca_lv_sl)
                    _sl_roi = calc_roi_pct(_sl_ep, curr_p, p.get("side", ""), LEVERAGE) if _sl_ep > 0 else 0

                    _t4_active = p.get("t4_defense", False)

                    if not _t4_active and _sl_roi <= _T4_ENTRY:
                        p["t4_defense"] = True
                        p["t4_worst_roi"] = _sl_roi
                        p["t4_defense_ts"] = now
                        _t4_active = True
                        _gap = 13.0 + _sl_roi
                        _tp = 2.0 * _sl_roi + 13.0
                        _dbg = f"[T3_DEF] ⚡ {symbol} {p.get('side','')} 방어모드 진입 roi={_sl_roi:.1f}% tp={_tp:.1f}%(갭{_gap:.0f})"
                        print(_dbg)
                        system_state.setdefault("_counter_tg", []).append(_dbg)

                    if _t4_active:
                        _t4_worst = float(p.get("t4_worst_roi", _sl_roi) or _sl_roi)
                        if _sl_roi < _t4_worst:
                            p["t4_worst_roi"] = _sl_roi
                            _t4_worst = _sl_roi

                        _t4_tp = 2.0 * _t4_worst + 13.0
                        _t4_gap = 13.0 + _t4_worst

                        if _sl_roi >= _t4_tp:
                            force = True
                            reason = f"T3_DEF_TP(worst={_t4_worst:.1f}%,tp={_t4_tp:.1f}%,gap={_t4_gap:.0f},roi={_sl_roi:.1f}%)"
                            _dbg = f"[T3_DEF] ✅ {symbol} 반등 탈출 roi={_sl_roi:.1f}% worst={_t4_worst:.1f}% gap={_t4_gap:.0f}"
                            print(_dbg)
                            system_state.setdefault("_counter_tg", []).append(_dbg)

                        elif _sl_roi <= _T4_HARD_SL:
                            force = True
                            reason = f"T3_DEF_SL(worst={_t4_worst:.1f}%,roi={_sl_roi:.1f}%)"

                # ── 기존 HARD_SL ──
                _t4_skip = (_dca_lv_sl >= 3 and p.get("t4_defense", False) and not force)
                if not force and not _t4_skip:
                    _sl_thresh = HARD_SL_BY_TIER.get(_dca_lv_sl, -4.0)
                    _sl_ep = get_sl_entry(p, _dca_lv_sl)

                    if _sl_ep > 0:
                        _sl_roi = calc_roi_pct(_sl_ep, curr_p, p.get("side", ""), LEVERAGE)
                        if _sl_roi <= _sl_thresh:
                            force  = True
                            reason = f"HARD_SL_T{_dca_lv_sl}({_sl_thresh}%,roi={_sl_roi:.1f}%)"
                            _hsl = system_state.setdefault("_hard_sl_history", [])
                            _hsl.append({"ts": time.time(), "side": p.get("side", "buy")})

                # ZOMBIE
                if not force:
                    _z_side = p.get("side", "buy")
                    _z_dca = int(p.get("dca_level", 1) or 1)
                    _z_slots = count_slots(st)
                    _z_full = (_z_slots.risk_long >= MAX_LONG if _z_side == "buy" else _z_slots.risk_short >= MAX_SHORT)
                    if _z_full and _z_dca < 3:
                        _zf, _zr = _zombie_exit(p, roi_pct, now,
                                                bad_regime_active=bad_regime_active,
                                                snapshot=snapshot)
                        if _zf:
                            force  = True
                            reason = _zr
                            _zombie_cooldown[_z_side] = now + ZOMBIE_COOLDOWN_SEC

                # ★ V10.30: ZOMBIE_TIMECUT 제거 — T2 회복 허용
                # 기존: T2 + 12h + ROI < 0 → 강제청산. BNB -1.4%에서 -$3 손절됨.
                # trim이 T2→T1 복귀 담당하므로 시간 기반 강제청산 불필요.

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
                # 배치 익절
                if "ZOMBIE" in reason and "TIMECUT" not in reason:
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
# 상태 영속화
# ═════════════════════════════════════════════════════════════════
def save_exit_state(system_state: dict):
    system_state["_zombie_cooldown"] = _zombie_cooldown


def restore_exit_state(system_state: dict):
    global _zombie_cooldown
    _zombie_cooldown = system_state.get("_zombie_cooldown", {"buy": 0.0, "sell": 0.0})
    print(f"[RESTORE] exit_engine: zombie_cd={_zombie_cooldown}")
    try:
        from v9.logging.logger_csv import log_system
        log_system("RESTORE", f"exit_engine zombie_cd_buy={_zombie_cooldown['buy']:.0f}")
    except Exception:
        pass
