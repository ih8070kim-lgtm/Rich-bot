"""
V9 Execution Engine  (v9.2 bugfix)
Intent 리스트를 우선순위 순으로 실행

[v9.2 BugFix]
  BUG1: OPEN intent 실행 전 active 상태 체크 없음
        → 이미 포지션이 있는 종목에 OPEN 재발동 → 중복 매수
        FIX:  OPEN 실행 전 st.get(sym, {}).get('active') 체크 추가
              active=True 이면 OPEN_ALREADY_ACTIVE 로그 후 스킵

  BUG2: OPEN TICK_LIMIT = 2 → 같은 틱에 2개 OPEN 허용
        FIX:  1로 낮춤 (과도한 진입 속도 방지)

  BUG3: 같은 방향 심볼 간 중복 체크 없음
        FIX:  executed_symbols 로직은 유지하되 OPEN에 한해 방향별 체크 추가
"""
import asyncio

from v9.execution.order_router import route_order
from v9.execution.position_book import get_p, is_active
from v9.logging.logger_csv import log_intent
from v9.types import INTENT_PRIORITY, Intent, IntentType, OrderResult

# ── Tick Limit 설정 ─────────────────────────────────────────────
# [BUG2 FIX] OPEN: 2 → 1  (틱당 최대 1건으로 제한, 중복진입 방지)
_TICK_LIMITS = {
    IntentType.OPEN:        1,
    IntentType.DCA:         2,
    IntentType.TP2:         None,
    IntentType.FORCE_CLOSE: None,
    IntentType.CLOSE:       None,
    IntentType.TP1:         None,
    IntentType.TRAIL_ON:    None,
}


async def execute_intents(
    ex,
    intents: list[Intent],
    dry_run: bool = False,
    st: dict = None,
) -> list[OrderResult]:
    """
    승인된 Intent 목록을 우선순위(P0~P6) 순으로 실행.
    상호배타: 같은 심볼에 대해 틱당 1개만 실행.
    Tick Limit: OPEN/DCA 는 틱당 최대 제한 (초과 시 SKIP_TICK_LIMIT 로그).
    [BUG1 FIX] OPEN 실행 전 이미 active 포지션 있으면 차단.
    """
    approved = [i for i in intents if i.approved]
    approved.sort(key=lambda x: INTENT_PRIORITY.get(x.intent_type, 99))

    results: list[OrderResult] = []
    executed_symbols = set()
    tick_counts: dict[IntentType, int] = {}

    for intent in approved:
        sym   = intent.symbol
        itype = intent.intent_type

        # 상호배타: 같은 심볼 중복 실행 방지
        _ex_key = f"{sym}:{intent.side}"
        if _ex_key in executed_symbols:
            continue

        # [BUG1 FIX] OPEN 전 이미 active 포지션 확인 ─────────────
        # planners가 active 체크를 했더라도, 이 레이어에서 이중 방어
        # HEDGE role은 반대방향 포지션이 정상이므로 체크 스킵
        _is_hedge_open = (itype == IntentType.OPEN and (intent.metadata or {}).get("role") == "HEDGE")
        if itype == IntentType.OPEN and st is not None and not _is_hedge_open:
            sym_st = st.get(sym, {})
            if get_p(sym_st, intent.side) is not None:
                print(f"[execution_engine] OPEN_ALREADY_ACTIVE {sym} — 스킵 (중복매수 방지)")
                log_intent(
                    trace_id=intent.trace_id,
                    intent_type=itype.value,
                    symbol=sym,
                    side=intent.side,
                    qty=intent.qty,
                    price=intent.price,
                    reason=f"OPEN_BLOCKED_ALREADY_ACTIVE",
                    approved=False,
                    reject_code="OPEN_ALREADY_ACTIVE",
                    role=(intent.metadata or {}).get("role", ""),
                    source_sym=(intent.metadata or {}).get("source_sym", ""),
                )
                continue

        # ★ v10.6: Stale Intent 방어 — 실행 직전 상태 재검증 ───────
        # intent 생성 시점과 실행 시점 사이에 같은 틱에서 상태가 바뀔 수 있음
        import time as _t
        _snap_ts = (intent.metadata or {}).get("snap_ts", 0)
        _intent_age = _t.time() - _snap_ts if _snap_ts else 0
        if _intent_age > 5.0:
            print(f"[execution_engine] STALE_INTENT {sym} {itype.value} age={_intent_age:.1f}s — 경고만")
        if st is not None:
            _sym_st_chk = st.get(sym, {})
            # ★ fix: TP1/TP2 intent.side는 청산 방향 — 포지션은 반대 방향
            if itype in (IntentType.TP1, IntentType.TP2):
                _chk_side = "sell" if intent.side == "buy" else "buy"
            else:
                _chk_side = intent.side
            _p_chk = get_p(_sym_st_chk, _chk_side)
            _stale_reason = None

            if itype == IntentType.TP1:
                # TP1: 이미 tp1_done이면 stale
                if _p_chk is None or _p_chk.get("tp1_done") or _p_chk.get("step", 0) != 0:
                    _stale_reason = "STALE_TP1_ALREADY_DONE"

            elif itype == IntentType.TP2:
                # TP2: step != 1 이거나 tp2_done이면 stale
                if _p_chk is None or _p_chk.get("tp2_done") or _p_chk.get("step", 0) != 1:
                    _stale_reason = "STALE_TP2_ALREADY_DONE"

            elif itype == IntentType.DCA:
                # DCA: intent tier가 현재 dca_level보다 커야 유효
                _intent_tier = (intent.metadata or {}).get("tier", 0)
                _curr_dca    = int((_p_chk or {}).get("dca_level", 0))
                if _p_chk is None:
                    _stale_reason = "STALE_DCA_NO_POSITION"
                elif _intent_tier > 0 and _intent_tier <= _curr_dca:
                    _stale_reason = f"STALE_DCA_ALREADY_T{_curr_dca}"

            if _stale_reason:
                print(f"[execution_engine] {_stale_reason} {sym} — intent 폐기")
                log_intent(
                    trace_id=intent.trace_id,
                    intent_type=itype.value,
                    symbol=sym,
                    side=intent.side,
                    qty=intent.qty,
                    price=intent.price,
                    reason=_stale_reason,
                    approved=False,
                    reject_code=_stale_reason,
                    role=(intent.metadata or {}).get("role", ""),
                    source_sym=(intent.metadata or {}).get("source_sym", ""),
                )
                continue

        # Tick Limit 체크 (HEDGE/CORE_HEDGE/INSURANCE_SH/TREND_COMP/BC 면제)
        _intent_role = (intent.metadata or {}).get("role", "")
        _entry_type = (intent.metadata or {}).get("entry_type", "")
        _is_exempt = (itype == IntentType.OPEN and (
            _intent_role in ("HEDGE", "CORE_HEDGE", "INSURANCE_SH", "BC", "CB")
            or _entry_type == "TREND"  # ★ V10.29d: TREND_COMP는 MR과 동시 진입 허용
        ))
        limit = _TICK_LIMITS.get(itype, None)
        if limit is not None and not _is_exempt:
            current_count = tick_counts.get(itype, 0)
            if current_count >= limit:
                print(f"[execution_engine] SKIP_TICK_LIMIT {sym} {itype.value} (limit={limit})")
                log_intent(
                    trace_id=intent.trace_id,
                    intent_type=intent.intent_type.value,
                    symbol=sym,
                    side=intent.side,
                    qty=intent.qty,
                    price=intent.price,
                    reason=f"SKIP_TICK_LIMIT:{itype.value}",
                    approved=False,
                    reject_code="SKIP_TICK_LIMIT",
                    role=(intent.metadata or {}).get("role", ""),
                    source_sym=(intent.metadata or {}).get("source_sym", ""),
                )
                continue
            tick_counts[itype] = current_count + 1

        # CSV 로그 (intent)
        log_intent(
            trace_id=intent.trace_id,
            intent_type=intent.intent_type.value,
            symbol=sym,
            side=intent.side,
            qty=intent.qty,
            price=intent.price,
            reason=intent.reason,
            approved=True,
            reject_code=intent.reject_code.value if intent.reject_code else "",
            role=(intent.metadata or {}).get("role", ""),
            source_sym=(intent.metadata or {}).get("source_sym", ""),
        )

        result = await route_order(ex, intent, dry_run=dry_run, st=st)
        results.append(result)

        if result.success:
            executed_symbols.add(f"{sym}:{intent.side}")
        else:
            _err_str = str(result.error or "")
            if "REDUCE_ONLY_REJECTED" in _err_str or "-2022" in _err_str:
                print(f"[execution_engine] -2022 감지: {sym} — open_fail_cooldown 필요 (runner에서 처리)")
            # ★ v10.5 fix: 최소수량 오류 연속 발생 시 이번 틱 차단 (무한루프 방지)
            if "must be greater than minimum amount precision" in _err_str:
                _fail_key = f"_qty_fail_{sym}"
                _fail_cnt = tick_counts.get(_fail_key, 0) + 1
                tick_counts[_fail_key] = _fail_cnt
                if _fail_cnt >= 2:
                    executed_symbols.add(f"{sym}:buy")
                    executed_symbols.add(f"{sym}:sell")
                    print(f"[execution_engine] {sym} 최소수량 오류 {_fail_cnt}회 → 이번 틱 전체 차단")

        await asyncio.sleep(0.1)

    # 거절된 Intent도 로그 기록
    for intent in intents:
        if not intent.approved:
            log_intent(
                trace_id=intent.trace_id,
                intent_type=intent.intent_type.value,
                symbol=intent.symbol,
                side=intent.side,
                qty=intent.qty,
                price=intent.price,
                reason=intent.reason,
                approved=False,
                reject_code=intent.reject_code.value if intent.reject_code else "",
                role=(intent.metadata or {}).get("role", ""),
                source_sym=(intent.metadata or {}).get("source_sym", ""),
            )

    return results
