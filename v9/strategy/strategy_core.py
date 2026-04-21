"""
V9 Strategy Core  (v10.27 — log_trade import 수정)
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

# ★ V10.31c: LEVERAGE / calc_roi_pct module-level import
# 과거 함수 내부에서 조건부(if/elif 내부) import했던 패턴 — 분기에 따라
# 특정 경로로 들어오면 NameError 발생 (예: apply_order_results line 467)
# 근본 해결: module-level에 고정하여 어느 실행 경로에서든 참조 가능
from v9.config import LEVERAGE
from v9.utils.utils_math import calc_roi_pct

# ★ V10.28b: Trim 선주문 취소 큐 (포지션 청산 시 runner가 처리)
_TRIM_CANCEL_QUEUE = []
# ★ V10.30: FC 시 거래소 전체 주문 취소 큐 (DCA_PRE 좀비 방지)
_FC_EXCHANGE_CANCEL = []

def get_trim_cancel_queue():
    """runner.py에서 호출 — 취소 대상 반환 후 클리어."""
    global _TRIM_CANCEL_QUEUE
    q = list(_TRIM_CANCEL_QUEUE)
    _TRIM_CANCEL_QUEUE.clear()
    return q

def get_fc_exchange_cancel():
    """★ V10.30: FC 후 거래소 전체 주문 취소 대상 반환 후 클리어."""
    global _FC_EXCHANGE_CANCEL
    q = list(_FC_EXCHANGE_CANCEL)
    _FC_EXCHANGE_CANCEL.clear()
    return q


def _tid() -> str:
    return str(uuid.uuid4())[:8]


def _check_hedge_sim_exit(sym: str, pos_side: str, exit_price: float,
                          system_state: dict, roi_pct: float = 0.0):
    """★ V10.29e: 동일심볼 헷지 시뮬레이션 — MR 청산 시 시뮬 PnL 비교."""
    if not system_state:
        return
    _hsim = system_state.get("_hedge_sim", {})
    _key = f"{sym}:{pos_side}"
    _sim = _hsim.pop(_key, None)
    if not _sim:
        return
    _sim_ep = _sim["ep"]
    _sim_side = _sim["side"]
    # ★ V10.31c: LEVERAGE module-level에서 import (line 24). 함수 내 중복 제거.
    if _sim_side == "buy":
        _sim_roi = (exit_price - _sim_ep) / _sim_ep * LEVERAGE * 100
    else:
        _sim_roi = (_sim_ep - exit_price) / _sim_ep * LEVERAGE * 100
    _hold_h = (time.time() - _sim.get("ts", time.time())) / 3600
    _tr_sym = _sim.get("trend_sym", "?")
    _tr_side = _sim.get("trend_side", "?")
    print(f"[HEDGE_SIM_EXIT] 📊 {sym} sim_{_sim_side} ep={_sim_ep:.4f} exit={exit_price:.4f} "
          f"sim_roi={_sim_roi:+.1f}% mr_roi={roi_pct:+.1f}% hold={_hold_h:.1f}h "
          f"(vs TREND {_tr_sym} {_tr_side})")
    try:
        from v9.logging.logger_csv import log_system
        log_system("HEDGE_SIM_EXIT", f"{sym} sim={_sim_roi:+.1f}% mr={roi_pct:+.1f}% "
                   f"vs_trend={_tr_sym} hold={_hold_h:.1f}h")
    except Exception:
        pass


def _check_trend_filter_sim_exit(sym: str, pos_side: str, exit_price: float,
                                 system_state: dict, roi_pct: float = 0.0):
    """★ V10.31c: BTC 방향성 필터 shadow logging — MR 청산 시 "필터 적용 시
    차단되었을 포지션"의 실현 ROI를 기록.
    
    목적: 필터가 실전에 들어가면 이득/손해였을지 사후 집계.
    양쪽 임계값(strict/loose)을 병렬 기록하여 어느 쪽이 유효한지 비교.
    """
    if not system_state:
        return
    _key = f"{sym}:{pos_side}"
    for _tag, _store_key in (("STRICT", "_trend_filter_sim_strict"),
                             ("LOOSE",  "_trend_filter_sim_loose")):
        _store = system_state.get(_store_key, {})
        _sim = _store.pop(_key, None)
        if not _sim:
            continue
        _hold_h = (time.time() - _sim.get("ts", time.time())) / 3600
        _btc_dir_at_entry = _sim.get("btc_dir", "?")
        _etype = _sim.get("entry_type", "?")
        # roi_pct는 실제 MR 포지션의 청산 ROI — 필터가 차단했다면 이 수익/손실은 없었음
        print(f"[TREND_FILTER_SIM_EXIT] 📊 {_tag} {sym} {pos_side} "
              f"blocked_roi={roi_pct:+.1f}% btc_was={_btc_dir_at_entry} "
              f"hold={_hold_h:.1f}h entry={_etype}")
        try:
            from v9.logging.logger_csv import log_system
            log_system("TREND_FILTER_SIM_EXIT",
                       f"{_tag} {sym} {pos_side} blocked_roi={roi_pct:+.1f}% "
                       f"btc_was={_btc_dir_at_entry} hold={_hold_h:.1f}h entry={_etype}")
        except Exception:
            pass


def apply_order_results(
    results: list[OrderResult],
    intents_map: dict,
    st: dict,
    cooldowns: dict,
    snapshot: MarketSnapshot,
    system_state: dict = None,
) -> None:
    """주문 결과를 포지션 북에 반영."""
    now = time.time()
    # ★ V10.31c: calc_roi_pct / LEVERAGE module-level에서 import (line 24-25)
    # 이전엔 함수 내 조건부 import로 UnboundLocalError/NameError 자주 터짐.
    # 근본 해결: module-level 단일 import → 함수 내 중복 제거

    # ★ CORE 포지션을 HEDGE보다 먼저 처리
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
            # ★ V10.31b: tp1_limit_oid 마킹 제거 — TP1이 시장가 trail로 전환됨
            # (TP1 limit_pending 발생 안 함 → 이 블록 도달 안 하지만 안전용 주석처리)
            # if (intent_pend and intent_pend.intent_type == IntentType.TP1 ...):
            #     _tp1_p["tp1_limit_oid"] = result.order_id
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
                "worst_roi":        0.0,
                "pending_dca":      None,
                "trailing_on_time": None,
                "hedge_mode":       False,
                "tp1_done":         False,
                "tp2_done":         False,
                "entry_type":       meta.get("entry_type", "MR"),
                "role":             meta.get("role", "CORE_MR"),
                "source_sym":       meta.get("source_sym", ""),
                "asym_forced":      meta.get("asym_forced", False),
                "locked_regime":    meta.get("locked_regime", "LOW"),
                "hedge_entry_price": meta.get("hedge_entry_price", 0.0),
                "t5_entry_price":   0.0,
                "insurance_timecut": meta.get("insurance_timecut", 0),
            })
            _log_pos(result.trace_id, sym, get_p(sym_st, intent.side), snapshot)
            # ★ V10.29: 새 진입 → 같은 방향 min_slot_hold 해제 (교체 완료)
            for _ms_sym, _ms_ss in st.items():
                if not isinstance(_ms_ss, dict) or _ms_sym == sym:
                    continue
                _ms_p = get_p(_ms_ss, intent.side)
                if isinstance(_ms_p, dict) and _ms_p.get("min_slot_hold"):
                    _ms_p["min_slot_hold"] = False
                    print(f"[MIN_SLOT] {_ms_sym} {intent.side} 교체 해제 ← 새 진입 {sym}")

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
                # ★ V10.29e FIX: DCA 시 stale TP1/trim 상태 클리어
                # T1 TP1 limit이 남아있으면 T2/T3 trim 영구 차단
                _stale_tp1_oid = p.pop("tp1_limit_oid", None)
                p.pop("tp1_preorder_id", None)
                p.pop("tp1_done", None)
                p["step"] = 0
                p["trailing_on_time"] = None
                # ★ V10.31e: DCA 전 max_roi tier별 보존 (측정 인프라)
                _pre_max = float(p.get("max_roi_seen", 0.0) or 0.0)
                _pre_tier = int(p.get("dca_level", 1) or 1)
                p.setdefault("max_roi_by_tier", {})[str(_pre_tier)] = _pre_max
                p["max_roi_seen"] = 0.0
                p["worst_roi"] = 0.0
                if _stale_tp1_oid:
                    _TRIM_CANCEL_QUEUE.append({"sym": sym, "oid": _stale_tp1_oid})
                    print(f"[DCA_FIX] {sym} stale tp1_limit_oid={_stale_tp1_oid} 취소큐 추가")
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
                            # ★ V10.31c: module-level calc_roi_pct 사용 (alias 제거)
                            _opp_roi = calc_roi_pct(
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
                p["pending_dca"] = None
                # ★ V10.29e: DCA 선주문 정리 (EP 변경 → 전부 취소, 다음 틱 재배치)
                for _dpt, _dpi in list(p.get("dca_preorders", {}).items()):
                    if _dpi.get("oid"):
                        _TRIM_CANCEL_QUEUE.append({"sym": sym, "oid": _dpi["oid"]})
                p["dca_preorders"] = {}
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
                # ★ V10.31c: ML 피처 로깅 복구 (plan_dca 제거 전엔 여기서 기록됨)
                try:
                    from v9.logging.logger_ml import record_ml_event
                    _real_bal_ml = float(getattr(snapshot, 'real_balance_usdt', 0) or 0)
                    record_ml_event(
                        trace_id=result.trace_id,
                        event_type=f"DCA_T{tier}",
                        p=p, sym=sym, snapshot=snapshot, st=st,
                        real_balance=_real_bal_ml, leverage=LEVERAGE,
                    )
                except Exception as _ml_e:
                    print(f"[ML_LOG] DCA 기록 실패(무시): {_ml_e}")
                # ★ V10.26: DCA 체결 → worst_roi/max_roi 0 리셋 (새 출발)
                # DCA = 새 ep 기준 새 게임. 이전 바닥/고점은 무의미.
                # ★ V10.31e: 리셋 전 tier별 max_roi 보존 (측정 인프라)
                _pre_max = float(p.get("max_roi_seen", 0.0) or 0.0)
                _pre_tier = int(p.get("dca_level", 1) or 1)
                p.setdefault("max_roi_by_tier", {})[str(_pre_tier)] = _pre_max
                p["worst_roi"] = 0.0
                p["max_roi_seen"] = 0.0
                # ★ V10.29e FIX: market DCA도 trim_to_place 세팅
                # limit DCA는 runner._apply_pending_fill에서 처리하지만
                # market DCA(HIGH 레짐)는 여기 도달 → 여기서도 세팅 필요
                if tier >= 2 and tier <= 4:
                    from v9.config import calc_trim_price, calc_trim_qty
                    import time as _time_mod
                    _sc_trim_price = calc_trim_price(float(p["ep"]), pos_side, tier)
                    _sc_trim_bal = float(getattr(snapshot, 'real_balance_usdt', 0) or 0)
                    if _sc_trim_bal > 0:
                        from v9.strategy.planners import _mr_available_balance
                        _sc_trim_bal = _mr_available_balance(snapshot, st)
                    _sc_trim_mark = float((snapshot.all_prices or {}).get(sym, 0) or 0)
                    _sc_trim_qty = calc_trim_qty(
                        float(p["amt"]), tier,
                        ep=float(p["ep"]), bal=_sc_trim_bal,
                        mark_price=_sc_trim_mark)
                    if _sc_trim_qty <= 0:
                        _sc_trim_qty = filled
                    # ★ V10.31b: trim qty 디버그
                    print(f"[TRIM_DBG] {sym} T{tier} calc_trim_qty: "
                          f"amt={p['amt']:.1f} ep={p['ep']:.4f} "
                          f"bal=${_sc_trim_bal:.0f} mark=${_sc_trim_mark:.5f} "
                          f"→ qty={_sc_trim_qty:.1f} (잔량={p['amt']-_sc_trim_qty:.1f})")
                    # 구 tier trim 선주문 취소
                    for _old_t, _old_info in list(p.get("trim_preorders", {}).items()):
                        if _old_info.get("oid"):
                            _TRIM_CANCEL_QUEUE.append({"sym": sym, "oid": _old_info["oid"]})
                    p["trim_preorders"] = {}
                    p["trim_to_place"] = {
                        "tier": tier,
                        "price": round(_sc_trim_price, 8),
                        "qty": _sc_trim_qty,
                        "side": "sell" if pos_side == "buy" else "buy",
                        "entry_price": float(p["ep"]),
                        "_ts": _time_mod.time(),
                    }
                    print(f"[TRIM_PREP_SC] {sym} {pos_side} T{tier}: "
                          f"trim 준비 {_sc_trim_qty:.4f}@${_sc_trim_price:.4f} (ep={p['ep']:.4f})")
                print(f"[DCA_RESET] {sym} {pos_side} T{tier}: "
                      f"worst_roi=0 max_roi=0 (새 ep={p['ep']:.4f})")
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
                    # ★ V10.28b: trim 선주문 취소 큐
                    if p.get("trim_preorders"):
                        for _tc_tier, _tc_info in p.get("trim_preorders", {}).items():
                            _TRIM_CANCEL_QUEUE.append({"sym": sym, "oid": _tc_info.get("oid", "")})
                    # ★ V10.29c: pending DCA/TP limit 전부 취소
                    try:
                        from v9.execution.order_router import _PENDING_LIMITS
                        _ps = "LONG" if pos_side == "buy" else "SHORT"
                        for _oid, _info in list(_PENDING_LIMITS.items()):
                            if _info.get("sym") == sym and _info.get("positionSide") == _ps:
                                _TRIM_CANCEL_QUEUE.append({"sym": sym, "oid": _oid})
                    except Exception: pass
                    cooldowns[sym] = now + 900
                    # ★ V10.29e: 헷지 시뮬 종료 체크
                    _check_hedge_sim_exit(sym, pos_side, avg_px, system_state,
                                         roi_pct=meta.get("roi_gross", 0.0))
                    # ★ V10.31c: BTC 방향성 필터 shadow 종료 체크
                    _check_trend_filter_sim_exit(sym, pos_side, avg_px, system_state,
                                                 roi_pct=meta.get("roi_gross", 0.0))
                    clear_position(st, sym, pos_side)
                    _log_pos_closed(result.trace_id, sym, pos_side, snapshot)
                    continue
                # ★ V10.27c: DCA Trim — tier 복귀 + DCA 재활용
                if meta.get("is_trim"):
                    _target_tier = meta.get("target_tier", max(1, int(p.get("dca_level", 2)) - 1))
                    _old_tier = int(p.get("dca_level", 1))
                    p["dca_level"] = _target_tier
                    p["worst_roi"] = 0.0
                    p["max_roi_seen"] = 0.0
                    p["pending_dca"] = None
                    p["step"] = 0           # ★ V10.27e: trail 상태 리셋
                    p["tp1_done"] = False    # ★ V10.27e: TP1 재진입 허용
                    p["trailing_on_time"] = None  # ★ V10.27e: stale 타임아웃 방지
                    # ★ V10.27e: trim된 tier들의 entry_price 클리어 (연속 trim 오판 방지)
                    _ep_keys = {2: "t2_entry_price", 3: "t3_entry_price", 4: "t4_entry_price"}
                    for _t in range(_target_tier + 1, _old_tier + 1):
                        if _t in _ep_keys:
                            p[_ep_keys[_t]] = 0.0
                    # DCA targets 재생성
                    try:
                        from v9.strategy.planners import _build_dca_targets
                        from v9.config import DCA_WEIGHTS, GRID_DIVISOR, LEVERAGE as _TRIM_LEV
                        _trim_ep = float(p.get("ep", 0) or 0)
                        _trim_amt = float(p.get("amt", 0) or 0)
                        _cum_w = sum(DCA_WEIGHTS[:_target_tier])
                        _total_w = sum(DCA_WEIGHTS)
                        _grid_est = (_trim_ep * _trim_amt) / (_cum_w / _total_w) if _cum_w > 0 else _trim_ep * _trim_amt * 5
                        p["dca_targets"] = [
                            t for t in _build_dca_targets(_trim_ep, pos_side, _grid_est, p.get("locked_regime", "LOW"))
                            if t.get("tier", 0) > _target_tier
                        ]
                    except Exception as _te:
                        p["dca_targets"] = []
                        print(f"[DCA_TRIM] {sym} dca_targets 재생성 실패: {_te}")
                    print(f"[DCA_TRIM] {sym} {pos_side} T{_old_tier}→T{_target_tier} "
                          f"sold={filled:.1f} remain={p['amt']:.1f} ep={p.get('ep',0):.4f}")
                    # ★ V10.28b: 체결된 tier의 trim_preorders 제거 + 취소 큐
                    _trp = p.get("trim_preorders", {})
                    _removed = _trp.pop(_old_tier, None)
                    if _removed and _removed.get("oid"):
                        _TRIM_CANCEL_QUEUE.append({"sym": sym, "oid": _removed["oid"]})
                    if _target_tier <= 1:
                        for _rt, _ri in _trp.items():
                            if _ri.get("oid"):
                                _TRIM_CANCEL_QUEUE.append({"sym": sym, "oid": _ri["oid"]})
                        p["trim_preorders"] = {}
                    # ★ V10.29e: trim 후 DCA 선주문 전부 취소 → 다음 틱 재배치
                    for _dpt2, _dpi2 in list(p.get("dca_preorders", {}).items()):
                        if _dpi2.get("oid"):
                            _TRIM_CANCEL_QUEUE.append({"sym": sym, "oid": _dpi2["oid"]})
                    p["dca_preorders"] = {}
                    _log_pos(result.trace_id, sym, p, snapshot)
                    continue
                if meta.get("is_tp2"):
                    p["tp2_done"] = True
                    _log_pos(result.trace_id, sym, p, snapshot)
                else:
                    p["step"]             = 1
                    p["tp1_done"]         = True
                    p["tp1_price"]        = avg_px if avg_px > 0 else snapshot.all_prices.get(sym, 0.0)
                    p["trailing_on_time"] = now
                    # ★ V10.29e FIX: TP1 체결 시 max_roi 보존
                    # 기존: snapshot 시점 ROI로 덮어씀 → 가격 하락 시 max 손실
                    # 수정: 기존 tracked max와 현재 중 큰 값 유지
                    _tp1_roi = calc_roi_pct(p.get("ep", 0.0),
                                            snapshot.all_prices.get(sym, 0.0),
                                            p.get("side", ""), LEVERAGE)
                    p["max_roi_seen"] = max(_tp1_roi, float(p.get("max_roi_seen", 0.0)))
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
            if _closing_role not in ("HEDGE", "SOFT_HEDGE", "INSURANCE_SH", "BC"):
                cooldowns[sym] = now + (1800 if itype == IntentType.FORCE_CLOSE else 900)
            # ★ v10.8: HEDGE/SOFT_HEDGE 청산 → hedge_engine 위임
            if _closing_role in ("SOFT_HEDGE", "HEDGE") and p:
                from v9.engines.hedge_engine_v2 import apply_hedge_close
                apply_hedge_close(p, sym, avg_px, st, snapshot, now)
            # ★ v10.10: INSURANCE_SH — 소스 영향 없음, 그냥 닫기
            if p:
                try:
                    # ★ V10.31c: LEVERAGE / calc_roi_pct module-level import 사용
                    from v9.logging.logger_csv import log_trade
                    _ep   = float(p.get("ep", 0.0) or 0.0)
                    _amt  = float(p.get("amt", 0.0) or 0.0)
                    _side = p.get("side", pos_side)
                    _cpx  = float(avg_px if avg_px > 0 else (snapshot.all_prices or {}).get(sym, _ep))
                    _roi  = calc_roi_pct(_ep, _cpx, _side, LEVERAGE) if _ep > 0 and _cpx > 0 else 0.0
                    # ★ V10.31d-3 FIX: realizedPnl 부분값 오류 방어
                    # 배경: V10.31b에서 order_router가 order['trades']에서 realizedPnl 추출.
                    # 그러나 FORCE_CLOSE/TRAIL_ON 대량 체결 시 trades 배열에 첫 1~2 조각만
                    # 담겨 전체 PnL의 일부만 합산되는 케이스 확인 (APT T3_DEF_TRAIL: 실제 -$22 → 로그 -$0.64).
                    # 해법: 자체계산(_self)과 대조 — realizedPnl이 50% 미만이면 부분값으로 판단, _self 사용.
                    _rpnl = getattr(result, 'realized_pnl', 0.0) or 0.0
                    _raw = (_cpx - _ep) / _ep if _side == "buy" else (_ep - _cpx) / _ep
                    _self_pnl = _raw * _amt * _cpx
                    if _rpnl != 0.0 and abs(_rpnl) >= abs(_self_pnl) * 0.5:
                        # realizedPnl이 합리적 범위 → 정확(수수료·펀딩 반영)하므로 우선
                        _pnl = _rpnl
                        _pnl_source = "rpnl"
                    else:
                        # realizedPnl이 없거나 부분값 → 자체계산 사용 (gross, 수수료 별도)
                        _pnl = _self_pnl
                        _pnl_source = "self"
                        if _rpnl != 0.0:
                            print(f"[PNL_FIX] {sym} {pos_side} rpnl={_rpnl:.2f} vs self={_self_pnl:.2f} "
                                  f"→ self 사용 (rpnl 부분값 의심)", flush=True)
                    # ★ V10.31d: 수수료 — OrderResult.fee_usdt에서 읽기
                    _fee = float(getattr(result, 'fee_usdt', 0.0) or 0.0)
                    # ★ V10.31e: T1 DCA 직전 max_roi 추출 (max_roi_by_tier["1"])
                    _t1_pre = float(p.get("max_roi_by_tier", {}).get("1", 0.0) or 0.0)
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
                        fee_usdt=_fee,
                        t1_max_roi_pre_dca=_t1_pre,  # ★ V10.31e
                        worst_roi_seen=float(p.get("worst_roi", 0.0) or 0.0),  # ★ V10.31j
                    )
                except Exception as _lt_err:
                    print(f"[strategy_core] log_trade 오류(무시): {_lt_err}")
            # ★ v10.6: CORE 포지션 청산 시 반대방향 HEDGE/CORE_HEDGE에 orphan 플래그 세팅
            # ★ V10.28b: 포지션 청산 전 trim 선주문 취소 큐
            if p and p.get("trim_preorders"):
                for _tc_tier, _tc_info in p.get("trim_preorders", {}).items():
                    _TRIM_CANCEL_QUEUE.append({"sym": sym, "oid": _tc_info.get("oid", "")})
                print(f"[TRIM_CANCEL_Q] {sym} {pos_side} trim {len(p['trim_preorders'])}건 취소 대기")
            # ★ V10.29c: 포지션 청산 시 해당 심볼의 pending DCA/TP limit 전부 취소
            # 미취소 시 DCA limit이 체결되어 유령 포지션 생성 (ARB $150 잔존 버그)
            try:
                from v9.execution.order_router import _PENDING_LIMITS
                _ps = "LONG" if pos_side == "buy" else "SHORT"
                _dca_cancel_count = 0
                for _oid, _info in list(_PENDING_LIMITS.items()):
                    if _info.get("sym") == sym and _info.get("positionSide") == _ps:
                        _TRIM_CANCEL_QUEUE.append({"sym": sym, "oid": _oid})
                        _dca_cancel_count += 1
                if _dca_cancel_count:
                    print(f"[PENDING_CANCEL_Q] {sym} {pos_side} pending {_dca_cancel_count}건 취소 대기")
            except Exception as _pc_e:
                print(f"[PENDING_CANCEL_Q] {sym} 스캔 실패(무시): {_pc_e}")
            # ★ V10.30: 거래소 잔존 주문 전수 취소 (DCA_PRE 좀비 방지)
            # _PENDING_LIMITS만으로는 불완전 (재시작 시 유실)
            _ps_fc = "LONG" if pos_side == "buy" else "SHORT"
            _FC_EXCHANGE_CANCEL.append({"sym": sym, "positionSide": _ps_fc})
            # ★ V10.29e: 헷지 시뮬 종료 체크
            _check_hedge_sim_exit(sym, pos_side, avg_px, system_state,
                                 roi_pct=float(meta.get("roi_pct", 0.0) or 0.0))
            # ★ V10.31c: BTC 방향성 필터 shadow 종료 체크
            _check_trend_filter_sim_exit(sym, pos_side, avg_px, system_state,
                                         roi_pct=float(meta.get("roi_pct", 0.0) or 0.0))
            clear_position(st, sym, pos_side)
            _log_pos_closed(result.trace_id, sym, pos_side, snapshot)


# ═════════════════════════════════════════════════════════════════
# Logging helpers
# ═════════════════════════════════════════════════════════════════
def _log_pos(trace_id: str, sym: str, p: dict, snapshot: MarketSnapshot):
    try:
        curr_p = (snapshot.all_prices or {}).get(sym, p.get("ep", 0.0))
        ep = p.get("ep", 0.0)
        # ★ V10.31c: module-level LEVERAGE / calc_roi_pct 사용 (line 24-25)
        roi_pct = calc_roi_pct(ep, curr_p, p.get("side", ""), LEVERAGE)
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
