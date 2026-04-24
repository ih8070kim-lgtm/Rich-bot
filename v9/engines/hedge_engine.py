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

            # ★ V10.31s: BTC 대비 이탈률 관측 (로그 전용, 청산 미실행)
            # 목적: 알트 독자 급등(숏 불리)/독자 하락(롱 불리) 패턴 데이터 수집
            # 쌓은 데이터로 실제 손실 케이스와 상관 분석 → 임계 근거 확보
            # 실측 ARB 04-22: BTC +0.20% 평이, ARB +3.84% (숏 불리) — 예측 불가했으나
            #   이런 패턴이 얼마나 자주 일어나는지, 손실 케이스와 연관성 있는지 파악 필요
            # 
            # 로그 조건 (스팸 방지):
            #   임계 |adverse_excess| ≥ 1.0% 이고 포지션별 5분 1회만 기록
            # 
            # 필드: sym side roi duration_min alt_pct btc_pct adverse_excess
            _btc_price_now = float(getattr(snapshot, "btc_price", 0) or 0)
            if _btc_price_now > 0:
                _btc_entry = float(p.get("_btc_entry_price", 0) or 0)
                _ep = float(p.get("ep", 0) or 0)
                _entry_ts = float(p.get("time", 0) or 0)
                # 진입 시점 BTC 가격 최초 1회만 저장 (90분 초과 시 재설정 안 함 — 전체 관측)
                if _btc_entry == 0 and _btc_price_now > 0:
                    p["_btc_entry_price"] = _btc_price_now
                    _btc_entry = _btc_price_now
                
                # 이탈률 계산 (진입 시점 대비)
                if _btc_entry > 0 and _ep > 0 and _entry_ts > 0:
                    _alt_pct = (curr_p - _ep) / _ep * 100  # 알트 변화 %
                    _btc_pct = (_btc_price_now - _btc_entry) / _btc_entry * 100
                    # 봇 방향 기준 불리 이탈
                    if is_long:
                        # 롱: btc 상승(유리) - alt 상승(유리). 불리 = btc_pct - alt_pct 양수
                        _adverse_excess = _btc_pct - _alt_pct
                    else:
                        # 숏: alt 하락(유리) - btc 하락(유리). 불리 = alt_pct - btc_pct 양수
                        _adverse_excess = _alt_pct - _btc_pct
                    
                    _duration_min = (now - _entry_ts) / 60.0 if _entry_ts > 0 else 0
                    # 로그 쿨다운 — 포지션별 5분 1회
                    _last_log_ts = float(p.get("_btc_decouple_log_ts", 0) or 0)
                    if abs(_adverse_excess) >= 1.0 and (now - _last_log_ts) >= 300 and _duration_min >= 15:
                        p["_btc_decouple_log_ts"] = now
                        try:
                            from v9.logging.logger_csv import log_system
                            log_system("BTC_DECOUPLE_OBS",
                                       f"{symbol} {p.get('side','')} roi={roi_pct:+.2f}% "
                                       f"dur={_duration_min:.0f}min "
                                       f"alt={_alt_pct:+.2f}% btc={_btc_pct:+.2f}% "
                                       f"adverse={_adverse_excess:+.2f}%p")
                        except Exception:
                            pass

            # 잔량 정리
            _res_amt = float(p.get("amt", 0.0) or 0.0)
            _res_notional = _res_amt * curr_p
            _res_min_qty = SYM_MIN_QTY.get(symbol, SYM_MIN_QTY_DEFAULT)
            # ★ V10.30 FIX: float epsilon(2.84e-14) 차단 + reduce_fail 쿨다운 존중
            # ★ V10.31b FIX: 필드명 통일 (runner는 exit_fail_cooldown_until 세팅)
            # ★ V10.31n FIX: Binance min_qty 경계값 float 오차 케이스 방어
            # ★ V10.31AM: MIN_NOTIONAL 미달 잔량 강제 클리어 — 거래소 주문 불가능한 잔량은 book에서 제거
            # 근거: 실측 OP 68회 RESIDUAL_CLEANUP 무한루프 — amt=0.0999999999994543 × $0.124 = $0.012
            #       거래소 MIN_NOTIONAL ~$5 미만이라 주문 절대 불가, force_close APPROVED만 찍히고 거래소 거절
            _res_cd = float(sym_st.get("exit_fail_cooldown_until", 0) or 0)
            _res_below_min_qty = _res_amt < _res_min_qty * 0.9999
            _res_below_min_notional = _res_notional < 5.0  # Binance 대부분 심볼 MIN_NOTIONAL=$5
            if (_res_below_min_qty or _res_below_min_notional) and _res_amt > 0:
                # 주문 불가능한 잔량 — 포지션 book에서 강제 클리어
                try:
                    from v9.execution.position_book import clear_position
                    from v9.logging.logger_csv import log_system
                    clear_position(st, symbol, p.get("side", ""))
                    log_system("RESIDUAL_FORCE_CLEAR",
                               f"{symbol} {p.get('side','')} amt={_res_amt:.8f} "
                               f"notional=${_res_notional:.4f} "
                               f"reason={'min_qty' if _res_below_min_qty else 'min_notional'} "
                               f"→ book에서 제거 (거래소 주문 불가)")
                    print(f"[RESIDUAL_FORCE_CLEAR] {symbol} {p.get('side','')} "
                          f"notional=${_res_notional:.4f} 주문 불가 → book 클리어")
                except Exception as _rfc_e:
                    print(f"[RESIDUAL_FORCE_CLEAR] {symbol} 실패(무시): {_rfc_e}")
                # 이 틱은 skip (이미 clear 했으니 force 분기 불필요)
                continue
            elif _res_amt > _res_min_qty * 0.01 and _res_cd < now:
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

                # ★ V10.31c: HIGH 레짐에서는 defense 공식 대신 trail 방식 사용
                # 이유: HIGH는 변동성 크기 때문에 "2×worst+13" 공식이 HARD_SL 근처로 수렴하며
                # 반등이 와도 작은 반등에 못 벗어나 손실 확정됨. 큰 반등 파도를 trail로 타는게 합리적.
                _current_regime = str(system_state.get("_current_regime", "") or "")
                _use_trail_mode = (_current_regime == "HIGH")

                if _dca_lv_sl >= 3:
                    _sl_ep = get_sl_entry(p, _dca_lv_sl)
                    _sl_roi = calc_roi_pct(_sl_ep, curr_p, p.get("side", ""), LEVERAGE) if _sl_ep > 0 else 0

                    _t4_active = p.get("t4_defense", False)

                    if not _t4_active and _sl_roi <= _T4_ENTRY:
                        p["t4_defense"] = True
                        p["t4_worst_roi"] = _sl_roi
                        p["t4_defense_ts"] = now
                        p["t4_peak_roi"]   = _sl_roi  # ★ V10.31c: trail용 peak 추적
                        p["t4_mode"]       = "trail" if _use_trail_mode else "formula"
                        _t4_active = True
                        if _use_trail_mode:
                            _dbg = f"[T3_DEF] ⚡ {symbol} {p.get('side','')} 방어모드-TRAIL 진입 (HIGH 레짐) roi={_sl_roi:.1f}%"
                        else:
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

                        # ★ V10.31c: 저장된 mode 기준 분기 (진입 시점 레짐 고정 — 중간에 바뀌어도 일관성)
                        _t4_mode_saved = p.get("t4_mode", "formula")

                        # 공식 TP 계산 (두 mode 공용 — trail 활성 기준으로도 사용)
                        _t4_tp = 2.0 * _t4_worst + 13.0
                        _t4_gap = 13.0 + _t4_worst

                        if _t4_mode_saved == "trail":
                            # ── TRAIL 방식 (HIGH 레짐) ──
                            # 공식 TP에 "도달"하면 trail 활성 (즉시 청산 X)
                            # 활성 후 peak 추적 → gap 되돌림 시 청산
                            _t4_armed = bool(p.get("t4_trail_armed", False))
                            if not _t4_armed and _sl_roi >= _t4_tp:
                                p["t4_trail_armed"] = True
                                p["t4_peak_roi"] = _sl_roi
                                _t4_armed = True
                                _dbg = f"[T3_DEF] 🎯 {symbol} TRAIL 활성 roi={_sl_roi:.1f}% (tp={_t4_tp:.1f}% 도달)"
                                print(_dbg)
                                system_state.setdefault("_counter_tg", []).append(_dbg)

                            if _t4_armed:
                                _t4_peak = float(p.get("t4_peak_roi", _sl_roi) or _sl_roi)
                                if _sl_roi > _t4_peak:
                                    _t4_peak = _sl_roi
                                    p["t4_peak_roi"] = _t4_peak
                                # ★ V10.31c: trim_trail과 동일한 fixed 0.3%p
                                _TRAIL_GAP = 0.3
                                if _sl_roi <= _t4_peak - _TRAIL_GAP:
                                    force = True
                                    reason = f"T3_DEF_TRAIL(worst={_t4_worst:.1f}%,peak={_t4_peak:.1f}%,exit={_sl_roi:.1f}%)"
                                    _dbg = f"[T3_DEF] ✅ {symbol} TRAIL 탈출 peak={_t4_peak:.1f}% → exit={_sl_roi:.1f}%"
                                    print(_dbg)
                                    system_state.setdefault("_counter_tg", []).append(_dbg)

                            if not force and _sl_roi <= _T4_HARD_SL:
                                force = True
                                reason = f"T3_DEF_SL(worst={_t4_worst:.1f}%,roi={_sl_roi:.1f}%)"
                        else:
                            # ── 기존 FORMULA 방식 (LOW/NORMAL) — TP 도달 시 즉시 청산 ──
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

                # ★ V10.31b: ZOMBIE 제거 — 슬롯풀이어도 trim/trail이 정리 담당
                # 기존: 슬롯풀 + T1/T2 + 조건 → 강제청산. 회복 차단 요인.

                # ★ V10.30: ZOMBIE_TIMECUT 제거 — T2 회복 허용
                # 기존: T2 + 12h + ROI < 0 → 강제청산. BNB -1.4%에서 -$3 손절됨.
                # trim이 T2→T1 복귀 담당하므로 시간 기반 강제청산 불필요.

            if force:
                # ★ V10.31s: 청산 시점 BTC 대비 이탈률 최종 기록 (관측용)
                try:
                    _btc_entry_log = float(p.get("_btc_entry_price", 0) or 0)
                    _ep_log = float(p.get("ep", 0) or 0)
                    _entry_ts_log = float(p.get("time", 0) or 0)
                    _btc_now_log = float(getattr(snapshot, "btc_price", 0) or 0)
                    if _btc_entry_log > 0 and _ep_log > 0 and _btc_now_log > 0 and _entry_ts_log > 0:
                        _alt_p_close = (curr_p - _ep_log) / _ep_log * 100
                        _btc_p_close = (_btc_now_log - _btc_entry_log) / _btc_entry_log * 100
                        if is_long:
                            _adv_close = _btc_p_close - _alt_p_close
                        else:
                            _adv_close = _alt_p_close - _btc_p_close
                        _dur_close = (now - _entry_ts_log) / 60.0
                        from v9.logging.logger_csv import log_system
                        log_system("BTC_DECOUPLE_CLOSE",
                                   f"{symbol} {p.get('side','')} roi={roi_pct:+.2f}% "
                                   f"dur={_dur_close:.0f}min alt={_alt_p_close:+.2f}% "
                                   f"btc={_btc_p_close:+.2f}% adverse={_adv_close:+.2f}%p "
                                   f"reason={reason[:40]}")
                except Exception:
                    pass

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
