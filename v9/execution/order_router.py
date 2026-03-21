"""
V9 Execution - Order Router  (v10.11b)
주문 라우팅:
  매수(OPEN/DCA): limit 5분 → 미체결 시 취소 (시장가 전환 없음)
  매도(TP1/TRAIL/FC): 즉시 시장가
  HEDGE_CORE 진입: 즉시 시장가
  INSURANCE_SH: 즉시 시장가
"""
import asyncio
import time

from v9.config import LEVERAGE
# ── 매수 limit 대기 시간: 5분 → 미체결 시 취소 ──
BUY_LIMIT_TIMEOUT_SEC = 300
from v9.logging.logger_csv import log_fill, log_order
from v9.types import Intent, OrderResult
from v9.execution.position_book import get_p

# ── Idempotency Key 중복주문 방지 캐시 ─────────────────────────
# 키: symbol|intent_type|tier|side|price  / 값: 등록 시각(unix)
# ✅ 성공 시에만 기록, 실패 시 제거 → 재시도 허용
_DEDUP_CACHE: dict = {}

# [BUG2 FIX] TTL을 5s → 300s 로 변경

# ── [BUG-2 FIX] 미체결 주문 추적: TRAIL_ON/FORCE_CLOSE 전 선취소 ──
# {sym: [(order_id, intent_type_str), ...]}
_PENDING_ORDERS: dict = {}

def _register_pending(sym: str, order_id, itype_str: str):
    """limit placed 시 등록"""
    if order_id is None:
        return
    _PENDING_ORDERS.setdefault(sym, []).append((str(order_id), itype_str))

def _clear_pending(sym: str):
    """체결/청산 완료 시 해제"""
    _PENDING_ORDERS.pop(sym, None)

async def cancel_pending_orders(ex, sym: str):
    """
    TRAIL_ON / FORCE_CLOSE 실행 직전, pending limit 주문 전부 취소.
    TP1 미체결이 남아 있으면 TRAIL 전량 reduceOnly가 -2022로 거절됨.
    """
    orders = list(_PENDING_ORDERS.pop(sym, []))
    for oid, itype_s in orders:
        try:
            await asyncio.to_thread(ex.cancel_order, oid, sym)
            print(f"[order_router] cancel_pending {sym} oid={oid} ({itype_s})")
        except Exception as e:
            print(f"[order_router] cancel_pending {sym} oid={oid} 실패(무시): {e}")
# 이유: open_fail_cooldown_until = now + 300 과 일치시킴
#       5초 TTL이면 같은 심볼을 5초 후에 다시 OPEN 가능 → 중복매수 원인
DEDUP_TTL = 300


async def route_order(
    ex,
    intent: Intent,
    dry_run: bool = False,
    st: dict = None,
) -> OrderResult:
    """
    Intent를 실제 주문으로 라우팅 (v10.11b).
    - 매수(OPEN/DCA 일반): limit 5분 → 미체결 시 취소 (fallback 없음)
    - 매도(TP1/TRAIL/FC): 즉시 시장가
    - HEDGE_CORE/INSURANCE_SH 진입: 즉시 시장가
    """
    sym       = intent.symbol
    side      = intent.side
    qty       = intent.qty
    price     = intent.price
    trace_id  = intent.trace_id
    tag       = f"V9_{intent.intent_type.value}_{sym}"

    # ── v10.11b: 라우팅 모드 결정 ──
    _meta_role = (intent.metadata or {}).get("role", "")
    _meta_entry = (intent.metadata or {}).get("entry_type", "")
    _force_market = (
        _is_reduce(intent)                           # TP1, TRAIL_ON, FORCE_CLOSE → 시장가
        or _meta_role in ("CORE_HEDGE", "INSURANCE_SH")  # 헷지/보험 진입 → 시장가
        or _meta_entry == "HEDGE_CORE"               # 헷지 진입 → 시장가
    )
    order_type = 'market' if (_force_market or not price) else 'limit'

    # ── Idempotency Key 중복주문 방지 ──────────────────────────
    tier = intent.metadata.get('tier', 0) if intent.metadata else 0
    price_key = round(float(getattr(intent, "price", 0.0) or 0.0), 2)
    side_key  = str(getattr(intent, "side", "") or "")
    idem_key  = f"{sym}|{intent.intent_type.value}|{tier}|{side_key}|{price_key}"

    # 만료된 캐시 정리
    now_ts = time.time()
    expired = [k for k, v in _DEDUP_CACHE.items() if now_ts - v > DEDUP_TTL]
    for k in expired:
        del _DEDUP_CACHE[k]

    # ✅ 성공 시에만 DEDUP 기록 → 실패/예외 시 재시도 허용
    ts = _DEDUP_CACHE.get(idem_key)
    if ts:
        if (now_ts - ts) < DEDUP_TTL:
            print(f"[order_router] DEDUP {sym} {intent.intent_type.value} key={idem_key}")
            log_order(trace_id, sym, side, order_type, qty, price, tag, None, "DEDUP")
            return _fail(trace_id, sym, side, qty, order_type, tag, f"DEDUP:{idem_key}")
        else:
            _DEDUP_CACHE.pop(idem_key, None)  # 만료면 제거

    if dry_run:
        sim_price = price or 0.0
        _DEDUP_CACHE[idem_key] = time.time()
        log_order(trace_id, sym, side, order_type, qty, price, tag, "DRY_RUN", "dry_run")
        log_fill(trace_id, sym, side, sim_price, qty, tag, "DRY_RUN")
        return OrderResult(
            trace_id=trace_id,
            success=True,
            order_id="DRY_RUN",
            symbol=sym,
            side=side,
            qty=qty,
            avg_price=sim_price,
            filled_qty=qty,
            order_type=order_type,
            tag=tag,
        )

    # ── set_leverage: LEVERAGE = 3 (정수) ──────────────────────


    # 추가: 실패 시 1회 재시도, 그래도 실패 시 OPEN은 에러 반환 (주문 진행 X)
    try:
        lev_int = int(LEVERAGE)
        await asyncio.to_thread(ex.set_leverage, lev_int, sym)
    except Exception as _lev_err:
        lev_err_str = str(_lev_err)
        # -4046: 이미 동일 레버리지 설정됨 → 무시하고 진행
        # 그 외: 1회 재시도
        if "-4046" in lev_err_str or "leverage not modified" in lev_err_str.lower():
            pass  # 이미 설정됨, 무시
        else:
            print(f"[order_router] set_leverage 1차 실패 ({sym}): {lev_err_str[:80]} → 재시도")
            await asyncio.sleep(0.5)
            try:
                await asyncio.to_thread(ex.set_leverage, lev_int, sym)
            except Exception as _lev_err2:
                lev_err_str2 = str(_lev_err2)
                if "-4046" in lev_err_str2 or "leverage not modified" in lev_err_str2.lower():
                    pass  # 재시도도 "이미 설정됨" → OK
                else:
                    # OPEN 계열은 레버리지 없이 진행하면 위험 → 실패 처리
                    if intent.intent_type.name == "OPEN":
                        print(f"[order_router] set_leverage 재시도도 실패 → OPEN 차단: {sym}")
                        _record_fail_cooldown(st, sym, intent, time.time())
                        return _fail(trace_id, sym, side, qty, order_type, tag,
                                     f"SET_LEVERAGE_FAIL:{lev_err_str2[:60]}")
                    else:
                        print(f"[order_router] set_leverage 경고 ({sym}): {lev_err_str2[:60]} — 진행")

    try:
        safe_qty = float(ex.amount_to_precision(sym, qty))
        if safe_qty <= 0:
            return _fail(trace_id, sym, side, qty, order_type, tag, "qty<=0 after precision")

        # ★ v9.9: 헤지모드 positionSide 처리
        from v9.config import HEDGE_MODE
        if HEDGE_MODE:
            # positionSide는 intent.metadata에서 가져오거나 intent type으로 추론
            pos_side_meta = (intent.metadata or {}).get("positionSide")
            if pos_side_meta:
                params = {"positionSide": pos_side_meta}
            else:
                # 추론: OPEN/DCA는 side 기준, 청산류는 반대
                if _is_reduce(intent):
                    params = {"positionSide": "SHORT" if side == "buy" else "LONG"}
                else:
                    params = {"positionSide": "LONG" if side == "buy" else "SHORT"}
        else:
            params = {'reduceOnly': True} if _is_reduce(intent) else {}

        if order_type == 'limit' and price:
            safe_price = float(ex.price_to_precision(sym, price))
            order = await asyncio.to_thread(
                ex.create_order, sym, 'limit', side, safe_qty, safe_price, params=params
            )
            order_id = order.get('id')
            _DEDUP_CACHE[idem_key] = time.time()
            log_order(trace_id, sym, side, 'limit', safe_qty, safe_price, tag, order_id, 'placed')
            _register_pending(sym, order_id, intent.intent_type.value)

            # ── v10.11b: 5분 대기 → 미체결 시 취소 (market fallback 없음) ──
            deadline = time.time() + BUY_LIMIT_TIMEOUT_SEC
            filled_qty = 0.0
            avg_price  = 0.0

            while time.time() < deadline:
                await asyncio.sleep(5)
                try:
                    info = await asyncio.to_thread(ex.fetch_order, order_id, sym)
                    status = info.get('status', '')
                    filled_qty = float(info.get('filled', 0.0) or 0.0)
                    avg_price  = float(info.get('average', 0.0) or price or 0.0)
                    if status == 'closed':
                        _clear_pending(sym)
                        log_fill(trace_id, sym, side, avg_price, filled_qty, tag, order_id)
                        return OrderResult(
                            trace_id=trace_id, success=True, order_id=order_id,
                            symbol=sym, side=side, qty=safe_qty,
                            avg_price=avg_price, filled_qty=filled_qty,
                            order_type='limit', tag=tag,
                        )
                    if status in ('canceled', 'rejected'):
                        break
                except Exception:
                    continue  # 폴링 실패 시 재시도 (break→continue)

            # ── 타임아웃: 취소만, market 전환 없음 ──
            try:
                await asyncio.to_thread(ex.cancel_order, order_id, sym)
                print(f"[order_router] {sym} limit 5분 미체결 → 취소 (fallback 없음)")
                # cancel 성공 후에도 부분 체결 확인
                try:
                    info = await asyncio.to_thread(ex.fetch_order, order_id, sym)
                    filled_qty = float(info.get('filled', 0.0) or 0.0)
                    avg_price = float(info.get('average', 0.0) or price or 0.0)
                except Exception:
                    pass
            except Exception:
                # cancel 실패 → 이미 체결됐을 수 있음 → 재확인
                try:
                    info = await asyncio.to_thread(ex.fetch_order, order_id, sym)
                    status = info.get('status', '')
                    if status == 'closed':
                        filled_qty = float(info.get('filled', 0.0) or 0.0)
                        avg_price = float(info.get('average', 0.0) or price or 0.0)
                        print(f"[order_router] {sym} cancel 실패 → 이미 체결됨! qty={filled_qty}")
                        _clear_pending(sym)
                        log_fill(trace_id, sym, side, avg_price, filled_qty, tag, order_id)
                        return OrderResult(
                            trace_id=trace_id, success=True, order_id=order_id,
                            symbol=sym, side=side, qty=safe_qty,
                            avg_price=avg_price, filled_qty=filled_qty,
                            order_type='limit', tag=tag,
                        )
                except Exception:
                    pass
            _clear_pending(sym)

            if filled_qty > 0:
                # 부분 체결된 양만 반환
                log_fill(trace_id, sym, side, avg_price, filled_qty, tag + "_PARTIAL", order_id)
                return OrderResult(
                    trace_id=trace_id, success=True, order_id=order_id,
                    symbol=sym, side=side, qty=safe_qty,
                    avg_price=avg_price, filled_qty=filled_qty,
                    order_type='limit_partial', tag=tag,
                )

            # 완전 미체결 → 실패 반환 (진입 안 함)
            return _fail(trace_id, sym, side, qty, order_type, tag, "limit 5min no fill — cancelled")

        else:
            # [BUG-2 FIX] TRAIL_ON/FORCE_CLOSE: pending limit 선취소 후 전량 market
            from v9.types import IntentType as _IT2
            if intent.intent_type in (_IT2.TRAIL_ON, _IT2.FORCE_CLOSE):
                await cancel_pending_orders(ex, sym)
            order = await asyncio.to_thread(
                ex.create_order, sym, 'market', side, safe_qty, params=params
            )
            order_id  = order.get('id')
            avg_price = _extract_price(order, None)
            filled    = float(order.get('filled', safe_qty) or safe_qty)
            _DEDUP_CACHE[idem_key] = time.time()
            log_order(trace_id, sym, side, 'market', safe_qty, None, tag, order_id, 'filled')
            log_fill(trace_id, sym, side, avg_price, filled, tag, order_id)
            return OrderResult(
                trace_id=trace_id, success=True, order_id=order_id,
                symbol=sym, side=side, qty=safe_qty,
                avg_price=avg_price, filled_qty=filled,
                order_type='market', tag=tag,
            )

    except Exception as e:
        err_s = str(e)
        _DEDUP_CACHE.pop(idem_key, None)
        _record_fail_cooldown(st, sym, intent, time.time(), err_s)

        if "-2022" in err_s or "ReduceOnly" in err_s:
            print(f"[order_router] -2022 ReduceOnly rejected: {sym} — DEDUP 해제")
            return _fail(trace_id, sym, side, qty, order_type, tag, f"REDUCE_ONLY_REJECTED:{err_s}")
        return _fail(trace_id, sym, side, qty, order_type, tag, err_s)


def _record_fail_cooldown(st, sym, intent, now_t, err_s=""):
    """[BUG3 FIX] 실패 쿨다운 기록을 ensure_slot 후에 안전하게 수행"""
    try:
        if st is None:
            return
        # ensure_slot 대신 직접 초기화 (순환 import 방지)
        if sym not in st:
            st[sym] = {
                'active': False,
                'p': None,
                'pending_entry': None,
                'pending_exit': None,
                'last_ohlcv_time': 0,
            }
        sym_st = st[sym]

        if intent.intent_type.name == "OPEN":
            sym_st["open_fail_cooldown_until"] = now_t + 300
            sym_st["last_open_ts"] = now_t

        if err_s and ("-2022" in err_s or "ReduceOnly" in err_s):
            sym_st["reduce_fail_cooldown_until"] = now_t + 10

        st[sym] = sym_st
    except Exception as _rfc_e:
        print(f"[order_router] _record_fail_cooldown 오류(무시): {_rfc_e}")


def _extract_price(order: dict, fallback) -> float:
    p = order.get('average') or order.get('price') or fallback
    if p is None or p == 0:
        p = fallback or 0.0
    return float(p) if p else 0.0


def _is_reduce(intent: Intent) -> bool:
    from v9.types import IntentType
    # [BUG-4 FIX] TP2도 reduceOnly=True — 누락 시 TP2가 신규 포지션 오픈 가능
    return intent.intent_type in (
        IntentType.FORCE_CLOSE, IntentType.CLOSE,
        IntentType.TP1, IntentType.TP2, IntentType.TRAIL_ON,
    )


def _fail(trace_id, sym, side, qty, order_type, tag, error) -> OrderResult:
    print(f"[order_router] FAIL {sym} {side} qty={qty} err={error}")
    log_order(trace_id, sym, side, order_type, qty, None, tag, None, f"FAIL:{error}")
    return OrderResult(
        trace_id=trace_id, success=False, order_id=None,
        symbol=sym, side=side, qty=qty,
        avg_price=0.0, filled_qty=0.0,
        order_type=order_type, tag=tag, error=error,
    )
