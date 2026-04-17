"""
V9 Risk Manager  (v9.8 — 방향별 총 노출 캡 추가)
==================================================
v9.7 → v9.8:
  [기존]
  - KILLSWITCH_FREEZE_ALL_MR(0.9): _EXIT_TYPES 통과, 나머지 전면 차단
  - ASYM_FORCE 진입(asym_forced=True): MR 0.9 미만이면 킬스위치 예외
  - HARD_SL ATR_BASE: config.HARD_SL_ATR_BASE 참조로 통일
  - Pressure Relief 제거

  [v9.8 신규]
  R7: 방향별 총 노출 캡
    - 방향별 명목 > equity × 1.8 → 신규 진입 거부
    - 양방향 합산 > equity × 2.6 → 신규 진입 거부
    - ASYM_FORCE: 커버 비율 (0.7 ~ 1.5) 초과 시 추가 거부
  
  ASYM 포지션은 소스 포지션 청산 후 독립 포지션으로 존속 (강제청산 연동 없음)
"""
import time

from v9.config import (
    DCA_MIN_CORR,
    DD_SHUTDOWN_THRESHOLD,
    HEDGE_OPEN_CORR_MIN,
    OPEN_CORR_MIN,
    KILLSWITCH_BLOCK_ALL_MR,
    KILLSWITCH_BLOCK_NEW_MR,
    KILLSWITCH_FREEZE_ALL_MR,
    LEVERAGE,
    T4_MAX_LOSS_PCT,
    TOTAL_MAX_SLOTS,
    EXPOSURE_CAP_DIR,
    EXPOSURE_CAP_TOTAL,
    ASYM_COVER_RATIO_MIN,
    ASYM_COVER_RATIO_MAX,
)
from v9.logging.logger_csv import log_risk
from v9.risk.slot_manager import can_open_side, can_open_hard, count_slots
from v9.risk.exposure import (
    calc_directional_exposure,
    check_exposure_cap,
    check_asym_cover_ratio,
)
from v9.types import Intent, IntentType, MarketSnapshot, RejectCode
from v9.execution.position_book import get_p, iter_positions, is_active

# 청산 계열 — MR 0.9 이상에서도 반드시 통과
_EXIT_TYPES = {
    IntentType.FORCE_CLOSE,
    IntentType.CLOSE,
    IntentType.TP1,
    IntentType.TP2,
    IntentType.TRAIL_ON,
}


def evaluate_intent(
    intent: Intent,
    snapshot: MarketSnapshot,
    st: dict,
    cooldowns: dict,
    system_state: dict,
    dry_run: bool = False,
) -> Intent:
    sym   = intent.symbol
    itype = intent.intent_type
    side  = intent.side
    mr    = snapshot.margin_ratio
    slots = count_slots(st)

    sym_st    = st.get(sym, {})
    p         = get_p(sym_st, side) or {}
    step      = p.get("step", 0)
    dca_level = p.get("dca_level", 1)
    meta      = intent.metadata or {}

    def _reject(code: RejectCode, note: str = "") -> Intent:
        intent.reject_code = code
        intent.approved    = False
        _log(code.value, note)
        return intent

    def _approve() -> Intent:
        intent.reject_code = RejectCode.APPROVED
        intent.approved    = True
        _log(RejectCode.APPROVED.value)
        return intent

    def _log(rc: str, note: str = ""):
        log_risk(
            trace_id=intent.trace_id, symbol=sym,
            intent_type=itype.value, reject_code=rc,
            margin_ratio=mr,
            risk_slots_total=slots.risk_total,
            risk_slots_long=slots.risk_long,
            risk_slots_short=slots.risk_short,
            step=step, dca_level=dca_level, note=note,
        )

    # ── 최우선: FORCE_CLOSE 무조건 통과 ──────────────────────────
    if itype == IntentType.FORCE_CLOSE:
        return _approve()

    # ── R0: 스냅샷 유효성 ────────────────────────────────────────
    if not snapshot.valid:
        return _reject(RejectCode.REJECT_INVALID_SNAPSHOT, "snapshot invalid")

    # ── R1: DD 셧다운 ────────────────────────────────────────────
    shutdown_active = system_state.get("shutdown_active", False)
    if shutdown_active and time.time() < system_state.get("shutdown_until", 0.0):
        return _reject(RejectCode.REJECT_DD_SHUTDOWN_ACTIVE)

    baseline = snapshot.baseline_balance
    current  = snapshot.real_balance_usdt
    if baseline > 0 and current > 0:
        dd_pct = (current - baseline) / baseline
        if dd_pct <= DD_SHUTDOWN_THRESHOLD:
            return _reject(RejectCode.FORCE_DD_HARDCUT, f"dd={dd_pct * 100:.2f}%")

    # ── R2: Kill Switch ──────────────────────────────────────────
    if mr >= KILLSWITCH_FREEZE_ALL_MR:
        if itype in _EXIT_TYPES:
            return _approve()
        return _reject(
            RejectCode.REJECT_KILLSWITCH_BLOCK_DCA,
            f"SYSTEM_FREEZE mr={mr:.3f}",
        )

    is_asym = bool(meta.get("role") in ("BALANCE", "HEDGE", "SOFT_HEDGE") or meta.get("asym_forced", False))
    if not is_asym:
        if mr >= KILLSWITCH_BLOCK_ALL_MR:
            if itype in (IntentType.OPEN, IntentType.DCA):
                code = (
                    RejectCode.REJECT_KILLSWITCH_BLOCK_DCA
                    if itype == IntentType.DCA
                    else RejectCode.REJECT_KILLSWITCH_BLOCK_NEW
                )
                return _reject(code, f"mr={mr:.3f}")
        elif mr >= KILLSWITCH_BLOCK_NEW_MR:
            if itype == IntentType.OPEN:
                return _reject(RejectCode.REJECT_KILLSWITCH_BLOCK_NEW, f"mr={mr:.3f}")

    # ── R3: Toggle ──────────────────────────────────────────────
    use_long  = system_state.get("use_long",  True)
    use_short = system_state.get("use_short", True)
    if itype in (IntentType.OPEN, IntentType.DCA):
        if side == "buy"  and not use_long:
            return _reject(RejectCode.REJECT_TOGGLE_OFF, "use_long=False")
        if side == "sell" and not use_short:
            return _reject(RejectCode.REJECT_TOGGLE_OFF, "use_short=False")

    # ── R4: 슬롯 ────────────────────────────────────────────────
    if itype == IntentType.OPEN:
        _intent_role = meta.get("role", "CORE_MR")

        # ★ Beta Cycle: MR 슬롯과 독립 — BC 자체 상한만 체크
        if _intent_role == "BC":
            from v9.config import BC_MAX_POS
            _bc_count = 0
            for _bc_sym, _bc_sd in st.items():
                if isinstance(_bc_sd, dict):
                    _bc_p = _bc_sd.get("p_short")
                    if isinstance(_bc_p, dict) and _bc_p.get("role") == "BC":
                        _bc_count += 1
            if _bc_count >= BC_MAX_POS:
                return _reject(RejectCode.REJECT_SLOT_LIMIT, f"SLOTS:BC_CAP({_bc_count}/{BC_MAX_POS})")
        # ★ V10.29c: Crash Bounce — CB 자체 상한만 체크
        elif _intent_role == "CB":
            from v9.config import CB_MAX_POS
            _cb_count = 0
            for _cb_sym, _cb_sd in st.items():
                if isinstance(_cb_sd, dict):
                    _cb_p = _cb_sd.get("p_long")
                    if isinstance(_cb_p, dict) and _cb_p.get("role") == "CB":
                        _cb_count += 1
            if _cb_count >= CB_MAX_POS:
                return _reject(RejectCode.REJECT_SLOT_LIMIT, f"SLOTS:CB_CAP({_cb_count}/{CB_MAX_POS})")
        else:
            # ★ v10.15: 전체 하드캡 (BC 제외) 먼저 체크
            if slots.risk_total >= TOTAL_MAX_SLOTS:
                return _reject(RejectCode.REJECT_SLOT_LIMIT, "SLOTS:TOTAL_CAP")
            if _intent_role == "CORE_HEDGE":
                # HEDGE: CORE_HEDGE 카운트 ≤ MAX_HEDGE_SLOTS(3)
                from v9.config import MAX_HEDGE_SLOTS
                _h_slots = count_slots(st, role_filter="CORE_HEDGE")
                if _h_slots.risk_total >= MAX_HEDGE_SLOTS:
                    return _reject(RejectCode.REJECT_SLOT_LIMIT, f"SLOTS:HEDGE_CAP({_h_slots.risk_total}/{MAX_HEDGE_SLOTS})")
            elif is_asym:
                ok, reason = can_open_hard(slots, side)
                if not ok:
                    return _reject(RejectCode.REJECT_SLOT_LIMIT, f"SLOTS:{reason}")
            else:
                # MR: CORE_MR만 카운트, 방향당 MAX_MR_PER_SIDE(4)
                from v9.config import MAX_MR_PER_SIDE
                _mr_slots = count_slots(st, role_filter="CORE_MR")
                if side == "buy" and _mr_slots.risk_long >= MAX_MR_PER_SIDE:
                    return _reject(RejectCode.REJECT_SLOT_LIMIT, f"SLOTS:MR_LONG({_mr_slots.risk_long}/{MAX_MR_PER_SIDE})")
                if side == "sell" and _mr_slots.risk_short >= MAX_MR_PER_SIDE:
                    return _reject(RejectCode.REJECT_SLOT_LIMIT, f"SLOTS:MR_SHORT({_mr_slots.risk_short}/{MAX_MR_PER_SIDE})")

    # ── R5: T4 최대 손실 ────────────────────────────────────────
    if itype == IntentType.DCA and dca_level >= 4:
        curr_p = float((snapshot.all_prices or {}).get(sym, 0.0))
        ep     = float(p.get("ep", 0.0))
        if ep > 0 and curr_p > 0:
            from v9.utils.utils_math import calc_roi_pct
            roi_r5 = calc_roi_pct(ep, curr_p, side, LEVERAGE)
            if roi_r5 <= -(T4_MAX_LOSS_PCT * 100):
                return _reject(RejectCode.FORCE_T4_MAXLOSS_BREACH, f"roi={roi_r5:.2f}%")

    # ── R6: 쿨다운 / Corr ────────────────────────────────────────
    # ★ BC/CB는 자체 쿨다운 + excess return 사용 → MR 쿨다운/Corr 스킵
    # ★ V10.31b: TREND_COMP도 쿨다운 면제 (MR 시그널과 동시 진입 의도)
    _entry_type = meta.get("entry_type", "")
    if (itype in (IntentType.OPEN, IntentType.DCA) and not is_asym
            and meta.get("role") not in ("BC", "CB")
            and _entry_type != "TREND"):
        now      = time.time()
        cd_until = cooldowns.get(sym, 0.0)
        if now < cd_until:
            return _reject(RejectCode.REJECT_COOLDOWN, f"cooldown until {cd_until:.0f}")

        corr     = (snapshot.correlations or {}).get(sym, 1.0)
        # ★ V10.27d: OPEN은 OPEN_CORR_MIN(0.50), HEDGE만 0.6, DCA는 DCA_MIN_CORR
        if itype == IntentType.OPEN:
            _is_hedge_open = meta.get("role") in ("CORE_HEDGE", "HEDGE", "SOFT_HEDGE", "INSURANCE_SH")
            min_corr = HEDGE_OPEN_CORR_MIN if _is_hedge_open else OPEN_CORR_MIN
        else:
            min_corr = DCA_MIN_CORR
        if corr < min_corr:
            return _reject(RejectCode.REJECT_CORR_LOW, f"corr={corr:.3f}<{min_corr}")

    # ── Money Caps — v10.7: 단일주문캡/심볼노출캡 제거 (SOFT_HEDGE 등 오거절 방지)
    qty    = float(getattr(intent, "qty", 0.0) or 0.0)
    equity = float(snapshot.real_balance_usdt or 0.0)
    px     = float((snapshot.all_prices or {}).get(sym, 0.0) or 0.0)

    # DCA qty 역산 (R7 exposure cap에서 필요)
    if itype == IntentType.DCA and qty <= 0:
        tier = meta.get("tier")
        p0 = get_p(st.get(sym, {}), side) or {}
        for tgt in (p0.get("dca_targets", []) or []):
            if not isinstance(tgt, dict) or tgt.get("tier") != tier:
                continue
            if float(tgt.get("qty",      0.0) or 0.0) > 0: qty = float(tgt["qty"]);      break
            not_val = float(tgt.get("notional", 0.0) or 0.0)
            if not_val > 0 and px > 0:                      qty = not_val / px;            break
        if qty <= 0:
            return _reject(RejectCode.REJECT_SLOT_LIMIT, "MISSING_QTY_FOR_CAPS_DCA")

    new_notional = qty * px if (qty > 0 and px > 0) else 0.0

    # ── R7: 방향별 총 노출 캡 (v9.8 신규, HEDGE/SOFT_HEDGE 면제) ──
    _r7_role = (meta or {}).get("role", "")
    _r7_exempt = _r7_role in ("HEDGE", "SOFT_HEDGE")
    if itype in (IntentType.OPEN, IntentType.DCA) and equity > 0 and new_notional > 0 and not _r7_exempt:
        prices = snapshot.all_prices or {}
        long_exp, short_exp = calc_directional_exposure(st, prices)

        # 7-A: 방향별 캡 + 양방향 합산 캡
        ok_cap, cap_reason = check_exposure_cap(
            side           = side,
            add_notional   = new_notional,
            long_notional  = long_exp,
            short_notional = short_exp,
            equity         = equity,
            cap_dir        = EXPOSURE_CAP_DIR,
            cap_total      = EXPOSURE_CAP_TOTAL,
        )
        if not ok_cap:
            return _reject(RejectCode.REJECT_EXPOSURE_CAP, cap_reason)

        # 7-B: ASYM 커버 비율 검사 (BALANCE 전용 — HEDGE/SOFT_HEDGE 면제)
        _r7b_role = (meta or {}).get("role", "")
        if is_asym and _r7b_role not in ("HEDGE", "SOFT_HEDGE"):
            ok_asym, asym_reason = check_asym_cover_ratio(
                asym_side        = side,
                add_notional     = new_notional,
                long_notional    = long_exp,
                short_notional   = short_exp,
                cover_ratio_min  = ASYM_COVER_RATIO_MIN,
                cover_ratio_max  = ASYM_COVER_RATIO_MAX,
            )
            if not ok_asym:
                return _reject(RejectCode.REJECT_ASYM_COVER_RATIO, asym_reason)

    return _approve()


# ═════════════════════════════════════════════════════════════════
# CorrGuard
# ═════════════════════════════════════════════════════════════════
CORR_GUARD_CORR_THRESH = 0.5
CORR_GUARD_ROI_THRESH  = -4.0
CORR_GUARD_CHECK_SEC   = 300
CORR_GUARD_BREACH_SEC  = 60

import uuid as _uuid


def generate_corrguard_intents(
    snapshot: MarketSnapshot,
    st: dict,
    system_state: dict,
) -> list:
    """
    CorrGuard 강제청산 Intent.
    - corr < 0.5 AND roi ≤ -4% AND 5분 주기 breach 기록 → 60초 후 limit
    - 60초 경과 → market fallback
    """
    from v9.utils.utils_math import calc_roi_pct

    intents  = []
    now_ts   = time.time()
    prices   = getattr(snapshot, "all_prices",  {}) or {}
    corr_map = getattr(snapshot, "correlations", {}) or {}

    last_chk = float(system_state.get("corr_guard_last_ts", 0.0) or 0.0)
    do_check = (now_ts - last_chk >= CORR_GUARD_CHECK_SEC)
    if do_check:
        system_state["corr_guard_last_ts"] = now_ts

    breach_ts_map = system_state.setdefault("corr_guard_breach_ts", {})

    for sym, sym_st in (st or {}).items():
        if not (isinstance(sym_st, dict) and is_active(sym_st)):
            breach_ts_map.pop(sym, None)
            continue

        # corr은 심볼 단위 — 포지션 루프 밖에서 한 번만 체크
        corr = float(corr_map.get(sym, 1.0))
        cp   = float(prices.get(sym, 0.0) or 0.0)
        if cp <= 0:
            continue

        # [BUG-1+2 FIX] hedge mode 지원: 포지션별로 독립 판단
        for pos_side, p in iter_positions(sym_st):
            # ★ v10.6: HEDGE role은 CorrGuard 대상 제외 (헷지는 자체 exit 로직 사용)
            # ★ V10.29e: BC/CB 독립전략도 제외 (x1, 자체 SL/TP 사용)
            if p.get("role") in ("HEDGE", "CORE_HEDGE", "SOFT_HEDGE",
                                 "INSURANCE_SH", "BC", "CB"):
                continue
            ep      = float(p.get("ep", 0.0) or 0.0)
            roi_pct = calc_roi_pct(ep, cp, pos_side, LEVERAGE) if ep > 0 else 0.0

            # roi 조건 미달 → breach 해제
            if roi_pct > CORR_GUARD_ROI_THRESH:
                breach_ts_map.pop(f"{sym}:{pos_side}", None)
                continue

            # corr 조건 미달 → breach 해제
            if corr >= CORR_GUARD_CORR_THRESH:
                breach_ts_map.pop(f"{sym}:{pos_side}", None)
                continue

            # 두 조건 모두 충족 → breach 기록
            breach_key = f"{sym}:{pos_side}"
            if do_check:
                breach_ts_map.setdefault(breach_key, now_ts)
            if breach_key not in breach_ts_map:
                continue

            amt = float(p.get("amt", 0.0) or 0.0)
            if amt <= 0:
                breach_ts_map.pop(breach_key, None)
                continue

            if p.get("pending_close") or int(p.get("step", 0) or 0) >= 1:
                continue

            elapsed    = now_ts - breach_ts_map[breach_key]
            close_side = "sell" if pos_side == "buy" else "buy"  # [BUG-1 FIX] side → pos_side

            if elapsed < CORR_GUARD_BREACH_SEC:
                intents.append(Intent(
                    trace_id=str(_uuid.uuid4())[:8],
                    symbol=sym, side=close_side,
                    intent_type=IntentType.CLOSE,
                    qty=amt, price=cp,
                    reason="CORR_GUARD_LIMIT",
                    metadata={"corr": corr, "roi_pct": roi_pct},
                ))
            else:
                intents.append(Intent(
                    trace_id=str(_uuid.uuid4())[:8],
                    symbol=sym, side=close_side,
                    intent_type=IntentType.FORCE_CLOSE,
                    qty=amt, price=None,
                    reason="CORR_GUARD_MARKET",
                    metadata={"corr": corr, "roi_pct": roi_pct},
                ))

    return intents
