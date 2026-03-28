"""
V9 Strategy Core  (v10.9.1 — SH/HH 소스 오염 방지 패치)
=========================================================
v10.9 → v10.9.1 변경:
  [BUG-SH1] OPEN: 기존 포지션 role 다르면 덮어쓰기 차단
  [BUG-SH2] apply_sh_open: source_side 누락 가드 + ep 교차검증
  [BUG-SH3] TRAIL_ON/CLOSE: role 교차검증 — 잘못된 side로 소스 삭제 방지
  [BUG-HH1] apply_hedge_close: 소스 identity 검증 후 rolling 데이터 기록
"""
import time
import uuid

from v9.execution.position_book import (
    clear_position, ensure_slot, get_p, set_p,
    get_pending_entry, set_pending_entry, iter_positions, is_active,
)
from v9.logging.logger_csv import log_position
from v9.types import IntentType, MarketSnapshot, OrderResult


def _tid() -> str:
    return str(uuid.uuid4())[:8]


def apply_order_results(
    results: list[OrderResult],
    intents_map: dict,
    st: dict,
    cooldowns: dict,
    snapshot: MarketSnapshot,
) -> None:
    """주문 결과를 포지션 북에 반영."""
    now = time.time()

    # ★ CORE 포지션을 HEDGE보다 먼저 처리 — source_sl_orphan 플래그 정합성 보장
    def _apply_order_key(r):
        _i = intents_map.get(r.trace_id)
        if _i and (_i.metadata or {}).get("role") == "HEDGE":
            return 1  # HEDGE 후순위
        return 0      # CORE 선순위
    results = sorted(results, key=_apply_order_key)

    for result in results:
        if not result.success:
            continue
        # ★ v10.14: limit_pending (fire-and-register) — filled=0은 runner가 추적
        # 단, OPEN intent는 pending_entry 세팅 필요 (다음 틱 중복 진입 방지)
        if result.filled_qty <= 0:
            intent_pend = intents_map.get(result.trace_id)
            if (intent_pend and intent_pend.intent_type == IntentType.OPEN
                    and getattr(result, 'order_type', '') == 'limit_pending'):
                _pend_sym = result.symbol
                _pend_side = intent_pend.side
                ensure_slot(st, _pend_sym)
                set_pending_entry(st[_pend_sym], _pend_side, {
                    "side": _pend_side,
                    "trace_id": result.trace_id,
                    "order_id": result.order_id,
                    "ts": now,
                })
            continue
        intent = intents_map.get(result.trace_id)
        if not intent:
            continue

        sym    = result.symbol
        itype  = intent.intent_type
        avg_px = result.avg_price
        filled = result.filled_qty
        meta   = intent.metadata or {}

        ensure_slot(st, sym)
        sym_st = st[sym]

        # ── 포지션 조회 side 결정 ────────────────────────────────
        if itype in (IntentType.TP1, IntentType.TP2,
                     IntentType.TRAIL_ON, IntentType.FORCE_CLOSE, IntentType.CLOSE):
            pos_side = "sell" if intent.side == "buy" else "buy"
        else:
            pos_side = intent.side

        p = get_p(sym_st, pos_side) or {}

        # ── OPEN ────────────────────────────────────────────────
        if itype == IntentType.OPEN:
            set_pending_entry(sym_st, intent.side, None)

            # ★ [BUG-SH1] 기존 포지션 보호 — role이 다르면 덮어쓰기 차단
            existing = get_p(sym_st, intent.side)
            if isinstance(existing, dict):
                _new_role = meta.get("role", "CORE_MR")
                _old_role = existing.get("role", "")
                # 같은 role이면 정상 교체 (재진입 등)
                # 다른 role이면 소스 오염 가능 → 차단
                if _old_role and _new_role != _old_role:
                    print(f"[GUARD] {sym} {intent.side} 기존 {_old_role} 보호 — "
                          f"신규 {_new_role} OPEN 무시 (소스 오염 방지)")
                    continue

            set_p(sym_st, intent.side, {
                "symbol":           sym,
                "side":             intent.side,
                "ep":               avg_px if avg_px > 0 else snapshot.all_prices.get(sym, 0.0),
                "original_ep":      avg_px if avg_px > 0 else snapshot.all_prices.get(sym, 0.0),
                "amt":              filled,
                "time":             now,
                "last_dca_time":    now,
                "atr":              meta.get("atr", 0.0),
                "tag":              f"V9_OPEN_{sym}",
                "step":             0,
                "dca_level":        meta.get("dca_level", 1),
                "dca_targets":      meta.get("dca_targets", []),
                "max_roi_seen":     0.0,
                "worst_roi":        0.0,  # ★ v10.14c: min_roi 반등 TP1용
                "pending_dca":      None,
                "trailing_on_time": None,
                "hedge_mode":       False,
                "open_cooldown_until": now + 15,
                "tp1_done":         False,
                "tp2_done":         False,
                "entry_type":       meta.get("entry_type", "MR"),
                "role":             meta.get("role", "CORE_MR"),
                "source_sym":       meta.get("source_sym", ""),
                "asym_forced":      meta.get("asym_forced", False),
                "last_hedge_exit_p":    0.0,
                "last_hedge_exit_side": "",
                "hedge_rolling_count":  0,
                "source_sl_orphan":     False,
                "locked_regime":        meta.get("locked_regime", "LOW"),
                "hedge_entry_price":    meta.get("hedge_entry_price", 0.0),
                "t5_entry_price":       0.0,
                "sh_trigger":           False,
                # ★ v10.10: INSURANCE_SH 타임컷
                "insurance_timecut":    meta.get("insurance_timecut", 0),
            })
            _log_pos(result.trace_id, sym, get_p(sym_st, intent.side), snapshot)

        # ── DCA ─────────────────────────────────────────────────
        elif itype == IntentType.DCA:
            # ★ v10.11b: DCA 적용 전 상태 기록 (디버그용)
            _dca_pre_amt = float(p.get("amt", 0)) if isinstance(p, dict) else 0
            _dca_pre_ep = float(p.get("ep", 0)) if isinstance(p, dict) else 0
            _dca_pre_lvl = int(p.get("dca_level", 0)) if isinstance(p, dict) else 0
            _dca_tier = meta.get("tier", 0)

            # ★ v10.11: role 교차검증 — 소스 DCA가 헷지에 적용되는 것 방지
            _dca_role = meta.get("_expected_role", "")
            if _dca_role and isinstance(p, dict) and p.get("role","") != _dca_role:
                print(f"[DCA_GUARD] {sym} {pos_side} role 불일치! "
                      f"기대={_dca_role} 실제={p.get('role')} → DCA 차단")
                continue
            if p and avg_px > 0 and filled > 0:
                total_cost  = (p["amt"] * p["ep"]) + (filled * avg_px)
                p["amt"]   += filled
                p["ep"]     = total_cost / p["amt"] if p["amt"] > 0 else avg_px
                tier        = meta.get("tier", p.get("dca_level", 1) + 1)
                p["dca_level"]     = tier
                p["last_dca_time"] = now
                p["time"]          = now
                p["dca_targets"]   = [
                    t for t in p.get("dca_targets", []) if t.get("tier") != tier
                ]
                if tier == 5:
                    p["t5_entry_price"] = avg_px
                    p["max_dca_reached"] = True
                    # ★ PATCH: T5 도달 즉시 헷지 미니게임 전환 (소스 청산 대기 X)
                    _opp_side = "sell" if pos_side == "buy" else "buy"
                    _opp_p = get_p(sym_st, _opp_side)
                    if isinstance(_opp_p, dict) and _opp_p.get("role") == "CORE_HEDGE":
                        _opp_p["t5_split"] = True
                        # 미니게임 즉시 시작
                        _opp_cp = float((snapshot.all_prices or {}).get(sym, 0.0) or 0.0)
                        if not _opp_p.get("t5_mini_active") and _opp_cp > 0:
                            from v9.utils.utils_math import calc_roi_pct as _crp
                            _opp_roi = _crp(
                                float(_opp_p.get("ep", 0) or 0),
                                _opp_cp,
                                _opp_p.get("side", "buy"),
                                3.0
                            )
                            _opp_p["t5_mini_active"] = True
                            _opp_p["t5_mini_start_price"] = _opp_cp
                            _opp_p["role"] = "CORE_MR"
                            _opp_p["source_sym"] = ""
                            _opp_p["source_side"] = ""
                            _opp_p["entry_type"] = "MR"
                            _opp_p["dca_targets"] = []
                            _opp_p["max_dca_reached"] = True
                            _opp_p["t5_mini_alpha"] = 1.0
                            _opp_p["worst_roi"] = _opp_roi
                            _opp_p["max_roi_seen"] = max(_opp_roi, float(_opp_p.get("max_roi_seen", 0) or 0))
                            print(f"[T5_MINI] {sym} 소스 T5 도달 → 헷지 {_opp_side} 미니게임 즉시 시작 "
                                  f"roi={_opp_roi:+.1f}% start_p={_opp_cp:.4f}")
                # ★ v10.12: 각 tier entry price 기록 (entry 기준 독립게임 TP1용)
                if tier == 2:
                    p["t2_entry_price"] = avg_px
                if tier == 3:
                    p["t3_entry_price"] = avg_px
                if tier == 4:
                    p["t4_entry_price"] = avg_px
                if tier >= 5:
                    p.setdefault("hedge_rolling_count", 0)
                p["pending_dca"] = None
                # ★ v10.15: DCA 체결 → insurance trigger 클리어 (정상 경로 도달)
                p["insurance_sh_trigger"] = None
                _cur_regime = meta.get("locked_regime", "")
                if _cur_regime:
                    from v9.strategy.planners import _wider_regime
                    p["locked_regime"] = _wider_regime(
                        p.get("locked_regime", "LOW"), _cur_regime
                    )
                # ★ v10.11b: DCA 적용 결과 확인 로그
                print(f"[DCA_APPLIED] {sym} {pos_side} T{tier}: "
                      f"qty {_dca_pre_amt:.1f}+{filled:.1f}={p['amt']:.1f} "
                      f"ep {_dca_pre_ep:.4f}→{p['ep']:.4f}")
                # ★ PATCH BUG2: DCA 체결 후 worst_roi 새 ep 기준으로 리셋
                # 구 ep 기준 worst_roi가 그대로 남으면 tp1_thresh가 비정상적으로 낮아져
                # DCA 직후 TP1이 즉시 발동하는 버그 방지
                try:
                    from v9.utils.utils_math import calc_roi_pct as _crp
                    _dca_new_roi = _crp(p["ep"], avg_px, p.get("side", "buy"), 3.0)
                    p["worst_roi"] = _dca_new_roi
                    # max_roi_seen도 새 ep 기준으로 재계산 (상단으로 올라온 경우 방지)
                    _dca_new_max = _crp(p["ep"], avg_px, p.get("side", "buy"), 3.0)
                    p["max_roi_seen"] = max(0.0, _dca_new_max)
                    print(f"[DCA_PATCH_WORST] {sym} {pos_side} T{tier}: "
                          f"worst_roi 리셋 {p.get('worst_roi',0):.2f}% (새 ep={p['ep']:.4f})")
                except Exception as _e:
                    print(f"[DCA_PATCH_WORST] worst_roi 리셋 실패(무시): {_e}")
                # ★ v10.10: sh_trigger 제거 — DCA_BLOCKED_INSURANCE로 대체
                _log_pos(result.trace_id, sym, p, snapshot)
            else:
                # ★ v10.11b: DCA 미적용 원인 로그
                print(f"[DCA_SKIP] {sym} {pos_side} T{_dca_tier}: "
                      f"p={bool(p)} avg_px={avg_px} filled={filled} "
                      f"pre_amt={_dca_pre_amt} pre_ep={_dca_pre_ep} pre_lvl={_dca_pre_lvl}")

        # ── TP1 / TP2 ──────────────────────────────────────────
        elif itype in (IntentType.TP1, IntentType.TP2):
            if p and filled > 0:
                p["amt"] = max(0.0, p["amt"] - filled)
                if p["amt"] <= 0:
                    cooldowns[sym] = now + 900
                    clear_position(st, sym, pos_side)
                    _log_pos_closed(result.trace_id, sym, pos_side, snapshot)
                    continue
                if meta.get("is_tp2"):
                    p["tp2_done"] = True
                    _log_pos(result.trace_id, sym, p, snapshot)
                else:
                    p["step"]             = 1
                    p["tp1_done"]         = True
                    p["tp1_price"]        = avg_px if avg_px > 0 else snapshot.all_prices.get(sym, 0.0)
                    p["trailing_on_time"] = now
                    _log_pos(result.trace_id, sym, p, snapshot)

        # ── TRAIL_ON / FORCE_CLOSE / CLOSE ──────────────────────
        elif itype in (IntentType.TRAIL_ON, IntentType.FORCE_CLOSE, IntentType.CLOSE):

            # ★ [BUG-SH3] role 교차검증 — 잘못된 side로 소스 삭제 방지
            # intent.metadata에 기대하는 role이 있으면, 실제 포지션 role과 비교
            _expected_role = meta.get("_expected_role")  # plan_trail_on에서 설정
            if _expected_role and isinstance(p, dict):
                _actual_role = p.get("role", "")
                if _actual_role != _expected_role:
                    print(f"[GUARD] {sym} {pos_side} role 불일치! "
                          f"기대={_expected_role} 실제={_actual_role} → 청산 차단")
                    continue

            _closing_role = (p.get("role", "") if p else "")
            if _closing_role not in ("HEDGE", "SOFT_HEDGE", "INSURANCE_SH"):
                cooldowns[sym] = now + (1800 if itype == IntentType.FORCE_CLOSE else 900)
            # ★ v10.8: HEDGE/SOFT_HEDGE 청산 → hedge_engine 위임
            if _closing_role in ("SOFT_HEDGE", "HEDGE") and p:
                from v9.engines.hedge_engine_v2 import apply_hedge_close
                apply_hedge_close(p, sym, avg_px, st, snapshot, now)
            # ★ v10.10: INSURANCE_SH — 소스 영향 없음, 그냥 닫기
            if p:
                try:
                    from v9.config import LEVERAGE as _LEV
                    from v9.utils.utils_math import calc_roi_pct
                    from v9.logging.logger_csv import log_trade
                    _ep   = float(p.get("ep", 0.0) or 0.0)
                    _amt  = float(p.get("amt", 0.0) or 0.0)
                    _side = p.get("side", pos_side)
                    _cpx  = float(avg_px if avg_px > 0 else (snapshot.all_prices or {}).get(sym, _ep))
                    _roi  = calc_roi_pct(_ep, _cpx, _side, _LEV) if _ep > 0 and _cpx > 0 else 0.0
                    _raw  = (_cpx - _ep) / _ep if _side == "buy" else (_ep - _cpx) / _ep
                    _pnl  = _raw * _amt * _cpx
                    log_trade(
                        trace_id=result.trace_id,
                        symbol=sym,
                        side=_side,
                        ep=_ep,
                        exit_price=_cpx,
                        amt=_amt,
                        pnl_usdt=_pnl,
                        roi_pct=_roi,
                        dca_level=int(p.get("dca_level", 1) or 1),
                        hold_sec=now - float(p.get("time", now) or now),
                        reason=itype.value,
                        hedge_mode=bool(p.get("hedge_mode", False)),
                        was_hedge=bool(p.get("was_hedge", False)),
                        max_roi_seen=float(p.get("max_roi_seen", 0.0) or 0.0),
                        entry_type=str(p.get("entry_type", "MR") or "MR"),
                        role=str(p.get("role", "") or ""),
                        source_sym=str(p.get("source_sym", "") or ""),
                    )
                except Exception as _lt_err:
                    print(f"[strategy_core] log_trade 오류(무시): {_lt_err}")
            # ★ v10.6: CORE 포지션 청산 시 반대방향 HEDGE에 orphan 플래그 세팅
            if p and _closing_role != "HEDGE":
                _opp_side_orphan = "sell" if pos_side == "buy" else "buy"
                _opp_p_orphan = get_p(st.get(sym, {}), _opp_side_orphan)
                if isinstance(_opp_p_orphan, dict) and _opp_p_orphan.get("role") == "HEDGE":
                    _opp_p_orphan["source_sl_orphan"] = True
                    print(f"[strategy_core] {sym} CORE 청산 → HEDGE source_sl_orphan 세팅")
            clear_position(st, sym, pos_side)
            _log_pos_closed(result.trace_id, sym, pos_side, snapshot)


# ═════════════════════════════════════════════════════════════════
# Logging helpers
# ═════════════════════════════════════════════════════════════════
def _log_pos(trace_id: str, sym: str, p: dict, snapshot: MarketSnapshot):
    try:
        curr_p = (snapshot.all_prices or {}).get(sym, p.get("ep", 0.0))
        ep = p.get("ep", 0.0)
        if ep > 0 and curr_p > 0:
            from v9.config import LEVERAGE
            raw_roi = (
                (curr_p - ep) / ep if p.get("side") == "buy"
                else (ep - curr_p) / ep
            )
            roi_pct = raw_roi * LEVERAGE * 100
        else:
            roi_pct = 0.0
        log_position(
            trace_id=trace_id, symbol=sym, side=p.get("side", ""),
            ep=ep, amt=p.get("amt", 0.0),
            dca_level=p.get("dca_level", 1), step=p.get("step", 0),
            roi_pct=roi_pct, max_roi_seen=p.get("max_roi_seen", 0.0),
            trailing_on=p.get("step", 0) >= 1,
            hedge_mode=p.get("hedge_mode", False),
            tag=p.get("tag", ""),
            role=str(p.get("role", "") or ""),
            source_sym=str(p.get("source_sym", "") or ""),
        )
    except Exception as _e:
        print(f"[strategy_core] _log_pos 오류(무시): {_e}")


def _log_pos_closed(trace_id: str, sym: str, side: str, snapshot: MarketSnapshot):
    try:
        log_position(
            trace_id=trace_id, symbol=sym, side=side,
            ep=0.0, amt=0.0, dca_level=0, step=0,
            roi_pct=0.0, max_roi_seen=0.0,
            trailing_on=False, hedge_mode=False, tag="CLOSED",
        )
    except Exception as _e:
        print(f"[strategy_core] _log_pos_closed 오류(무시): {_e}")


def snapshot_positions(st: dict, snapshot: MarketSnapshot) -> None:
    """매 틱 포지션 상태를 log_positions에 기록."""
    for sym, sym_st in st.items():
        for _, p in iter_positions(sym_st):
            _log_pos(_tid(), sym, p, snapshot)
