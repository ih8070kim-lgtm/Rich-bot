"""
V9 Execution - Order Router  (v10.13)
주문 라우팅:
  매수(OPEN/DCA): limit 5분 → 미체결 시 취소 (시장가 전환 없음)
  매도(TP1/TRAIL/FC): 즉시 시장가
  CORE_HEDGE 진입: limit (v10.13: maker 전환)
  INSURANCE_SH: 즉시 시장가
"""
import asyncio
import time

from v9.config import LEVERAGE
# ── ★ v10.14: limit 블로킹 제거 — runner._manage_pending_limits가 추적 ──
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
    ★ V10.29d: _PENDING_LIMITS도 같이 취소 (DCA limit 잔존 방지)
    """
    # (1) _PENDING_ORDERS 취소
    orders = list(_PENDING_ORDERS.pop(sym, []))
    for oid, itype_s in orders:
        try:
            await asyncio.to_thread(ex.cancel_order, oid, sym)
            print(f"[order_router] cancel_pending {sym} oid={oid} ({itype_s})")
        except Exception as e:
            print(f"[order_router] cancel_pending {sym} oid={oid} 실패(무시): {e}")

    # (2) _PENDING_LIMITS에서 같은 심볼 취소
    _to_cancel = [(oid, info) for oid, info in list(_PENDING_LIMITS.items())
                  if info.get("sym") == sym]
    for oid, info in _to_cancel:
        try:
            await asyncio.to_thread(ex.cancel_order, oid, sym)
            _PENDING_LIMITS.pop(oid, None)
            print(f"[order_router] cancel_pending_limit {sym} oid={oid} ({info.get('intent_type','')})")
        except Exception as e:
            _PENDING_LIMITS.pop(oid, None)  # 실패해도 레지스트리에서 제거
            print(f"[order_router] cancel_pending_limit {sym} oid={oid} 실패(무시): {e}")
# 이유: open_fail_cooldown_until = now + 300 과 일치시킴
#       5초 TTL이면 같은 심볼을 5초 후에 다시 OPEN 가능 → 중복매수 원인
DEDUP_TTL = 300

# ── v10.13: 비동기 limit 추적 레지스트리 ──────────────────────
# runner._manage_pending_limits()가 주기적으로 체결/타임아웃 확인
# {order_id: {sym, side, qty, price, trace_id, tag, placed_at, intent_type, positionSide}}
_PENDING_LIMITS: dict = {}
PENDING_LIMIT_TIMEOUT_SEC = 300  # 5분 미체결 시 취소


def _register_pending_limit(trace_id, sym, side, qty, price, order_id, tag, intent):
    """limit 주문을 PENDING 레지스트리에 등록 — runner가 추적
    ★ v10.14: full metadata 저장 (_apply_pending_fill 완전 반영용)
    """
    _meta = dict(intent.metadata) if (intent and intent.metadata) else {}
    _PENDING_LIMITS[str(order_id)] = {
        "sym": sym,
        "side": side,
        "qty": qty,
        "price": price,
        "trace_id": trace_id,
        "tag": tag,
        "placed_at": time.time(),
        "intent_type": intent.intent_type.value if intent else "UNKNOWN",
        "positionSide": _meta.get("positionSide", ""),
        "role": _meta.get("role", ""),
        # ★ v10.14: DCA/OPEN 완전 반영에 필요한 메타데이터
        "tier": _meta.get("tier", 0),
        "dca_targets": _meta.get("dca_targets", []),
        "locked_regime": _meta.get("locked_regime", "LOW"),
        "source_sym": _meta.get("source_sym", ""),
        "source_side": _meta.get("source_side", ""),
        "entry_type": _meta.get("entry_type", "MR"),
        "atr": _meta.get("atr", 0.0),
        "insurance_timecut": _meta.get("insurance_timecut", 0),
        "dca_level": _meta.get("dca_level", 1),
        "_expected_role": _meta.get("_expected_role", ""),
        # ★ V10.28: DCA Trim 메타데이터
        "is_trim": _meta.get("is_trim", False),
        "target_tier": _meta.get("target_tier", 0),
        "entry_price": _meta.get("entry_price", 0),  # ★ V10.31b: PnL 계산용
        "is_tp_pre": _meta.get("is_tp_pre", False),  # ★ V10.31b: 타임아웃 제외
        "is_dca_pre": _meta.get("is_dca_pre", False),
        # ★ V10.31e-8: 미장전 limit 재배치 추적용 (스텝별 취소 + 재배치)
        "is_pre_market_limit": _meta.get("is_pre_market_limit", False),
        # ★ V10.31f: T3 8h컷 limit 재배치 추적용
        "is_t3_8h_limit": _meta.get("is_t3_8h_limit", False),
        # ★ V10.31j: T3 3h컷 TREND 전용 limit 재배치 추적용
        "is_t3_3h_limit": _meta.get("is_t3_3h_limit", False),
        # ★ V10.31k: Portfolio TP limit 재배치 추적용 (step별 취소+재배치)
        "is_ptp_limit": _meta.get("is_ptp_limit", False),
    }
    print(f"[order_router] PENDING_LIMIT registered: {sym} {side} {qty}@{price} oid={order_id}")


def get_pending_limits() -> dict:
    """runner에서 접근용"""
    return _PENDING_LIMITS


def remove_pending_limit(order_id: str):
    """체결/취소 완료 시 제거"""
    _PENDING_LIMITS.pop(str(order_id), None)


async def route_order(
    ex,
    intent: Intent,
    dry_run: bool = False,
    st: dict = None,
) -> OrderResult:
    """
    Intent를 실제 주문으로 라우팅 (v10.13).
    - 매수(OPEN/DCA/CORE_HEDGE): limit 5분 → 미체결 시 취소
    - 매도(TP1/TRAIL/FC): 즉시 시장가
    - INSURANCE_SH 진입: 즉시 시장가
    """
    sym       = intent.symbol
    side      = intent.side
    qty       = intent.qty
    price     = intent.price
    trace_id  = intent.trace_id
    tag       = f"V9_{intent.intent_type.value}_{sym}"

    # ── v10.21: 라우팅 모드 결정 ──
    # TP1: 지정가 (수익 목표 도달 → 슬리피지 0)
    # TRAIL_ON/FORCE_CLOSE: 시장가 (급히 빠져야 → 즉시 체결)
    # ★ V10.31p: T3_3H_S0~S2 / T3_8H_S0~S2 / is_ptp_limit / is_trim_limit도 LIMIT 보존
    # 기존 버그: intent_type=CLOSE이면 무조건 시장가 → LIMIT 유리가격 의도 무효화
    # 실측 증거: AVAX T3_3H_S0 price=9.280 LIMIT 의도, filled @9.322 시장가 → -2.29% 손실
    _meta_role = (intent.metadata or {}).get("role", "")
    _meta_entry = (intent.metadata or {}).get("entry_type", "")
    _meta = intent.metadata or {}
    _is_step_limit = (
        bool(_meta.get("is_t3_3h_limit", False))
        or bool(_meta.get("is_t3_8h_limit", False))
        or bool(_meta.get("is_ptp_limit", False))
        or bool(_meta.get("is_trim_limit", False))
    )
    from v9.types import IntentType as _IT_route
    _force_market = (
        _is_reduce(intent) and intent.intent_type != _IT_route.TP1  # TP1 제외 — 지정가
        and not bool(_meta.get("pre_market_limit", False))  # ★ V10.31b: 미장전 limit
        and not _is_step_limit  # ★ V10.31p: 단계적 LIMIT도 제외
        or _meta_role in ("INSURANCE_SH", "CORE_HEDGE")
        or bool(_meta.get("force_market", False))
    )
    order_type = 'market' if (_force_market or not price) else 'limit'

    # ── Idempotency Key 중복주문 방지 ──────────────────────────
    tier = intent.metadata.get('tier', 0) if intent.metadata else 0
    side_key  = str(getattr(intent, "side", "") or "")
    # ★ v10.24 Fix E: TP1/TP2 DEDUP 키에서 price 제거
    # 가격이 미세 변동(2152.78→2153.82→2152.93)하면 DEDUP 미스 → 30초 내 6개 동시 배치
    from v9.types import IntentType as _IT_dedup
    if intent.intent_type in (_IT_dedup.TP1, _IT_dedup.TP2):
        idem_key = f"{sym}|{intent.intent_type.value}|{tier}|{side_key}"
    else:
        price_key = round(float(getattr(intent, "price", 0.0) or 0.0), 2)
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
            # ★ v10.14: print 제거 (CSV 로그는 유지, 콘솔 노이즈 감소)
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

    # ── set_leverage ──────────────────────
    # ★ V10.29d: BC는 1x, 나머지 3x
    _meta_role_lev = (intent.metadata or {}).get("role", "")
    lev_int = 1 if _meta_role_lev in ("BC", "CB") else int(LEVERAGE)

    # 추가: 실패 시 1회 재시도, 그래도 실패 시 OPEN은 에러 반환 (주문 진행 X)
    try:
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

            # ── ★ v10.14: fire-and-register — 블로킹 제거 ──
            # limit 배치 후 즉시 리턴, runner._manage_pending_limits가 체결 추적
            # filled_qty=0 → apply_order_results에서 스킵
            _register_pending_limit(trace_id, sym, side, safe_qty, safe_price, order_id, tag, intent)
            return OrderResult(
                trace_id=trace_id, success=True, order_id=order_id,
                symbol=sym, side=side, qty=safe_qty,
                avg_price=0.0, filled_qty=0.0,
                order_type='limit_pending', tag=tag,
            )

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

            # ★ V10.31b: 바이낸스 realizedPnl 추출 (reduce 주문만)
            # ★ V10.31d: commission(수수료)도 함께 추출 — reduce/open 모두
            # ★ V10.31d-3: limit 5→50 확대 — FORCE_CLOSE 대량 체결이 다수 조각으로 쪼개질 때
            #   첫 5건만 잡혀 realizedPnl 부분값 기록 문제 완화. strategy_core에 50% 검증 로직도 추가됨.
            _rpnl = 0.0
            _fee = 0.0
            try:
                _order_trades = order.get('trades') or []
                if not _order_trades:
                    # ccxt가 trades 안 줬으면 fetch
                    _order_trades = await asyncio.to_thread(
                        ex.fetch_my_trades, sym, limit=50)
                    _order_trades = [t for t in _order_trades
                                     if str(t.get('order', '')) == str(order_id)]
                for _t in _order_trades:
                    _info = _t.get('info') or {}
                    # realizedPnl은 reduce 주문에만 의미. open은 0
                    if _is_reduce(intent):
                        _rpnl += float(_info.get('realizedPnl', 0) or 0)
                    # commission: ccxt 'fee.cost' 또는 info.commission
                    _fee_obj = _t.get('fee') or {}
                    _fee_cost = float(_fee_obj.get('cost', 0) or 0)
                    if _fee_cost == 0:
                        _fee_cost = float(_info.get('commission', 0) or 0)
                    _fee += _fee_cost
            except Exception:
                pass

            return OrderResult(
                trace_id=trace_id, success=True, order_id=order_id,
                symbol=sym, side=side, qty=safe_qty,
                avg_price=avg_price, filled_qty=filled,
                order_type='market', tag=tag,
                realized_pnl=_rpnl,
                fee_usdt=_fee,
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

        # ★ V10.31c: precision / min-notional 에러도 쿨다운 (무한재시도 방지)
        # 청산 intent (TP1/TRAIL_ON/FORCE_CLOSE 등)가 precision 오류로 거부되면
        # 60초간 재시도 차단. dust는 가격 변동으로도 해결되므로 짧은 쿨다운 충분.
        if err_s:
            _err_low = err_s.lower()
            _precision_like = (
                "minimum amount precision" in _err_low
                or ("precision" in _err_low and "must be greater" in _err_low)
                or "-1111" in err_s
                or "-4003" in err_s
                or "-4005" in err_s
                or ("minimum" in _err_low and "notional" in _err_low)
            )
            if _precision_like:
                _exit_types = ("FORCE_CLOSE", "CLOSE", "TP1", "TP2", "TRAIL_ON")
                if intent.intent_type.name in _exit_types:
                    sym_st["exit_fail_cooldown_until"] = now_t + 60
                else:
                    sym_st["open_fail_cooldown_until"] = now_t + 60

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


# ═══════════════════════════════════════════════════════════════
# ★ V10.31c: 선주문 취소 헬퍼 (runner.py에서 이동)
# 주문 실행 책임을 order_router 단일 모듈에 집중
# ═══════════════════════════════════════════════════════════════

async def cancel_tp1_preorder(ex, p: dict, sym: str):
    """기존 TP1 선주문 취소 + 레지스트리 정리.
    
    ★ V10.31c: runner.py에서 이동. 주문 실행 책임을 order_router에 통합.
    """
    oid = p.get("tp1_preorder_id")
    if not oid or oid == "DRY_PREORDER":
        p["tp1_preorder_id"] = None
        p["tp1_preorder_price"] = None
        p["tp1_preorder_ts"] = None
        return
    try:
        import asyncio as _aio
        await _aio.to_thread(ex.cancel_order, oid, sym)
        print(f"[TP1_PRE] {sym} 선주문 취소 oid={oid}")
    except Exception as _e:
        _err = str(_e)
        if "Unknown order" in _err or "-2011" in _err:
            pass  # 이미 체결 또는 만료
        else:
            print(f"[TP1_PRE] {sym} 선주문 취소 실패: {_e}")
    # 레지스트리 정리
    try:
        remove_pending_limit(str(oid))
        _orders = _PENDING_ORDERS.get(sym, [])
        _PENDING_ORDERS[sym] = [(o, t) for o, t in _orders if o != str(oid)]
        if not _PENDING_ORDERS[sym]:
            _PENDING_ORDERS.pop(sym, None)
    except Exception:
        pass
    p["tp1_preorder_id"] = None
    p["tp1_preorder_price"] = None
    p["tp1_preorder_ts"] = None


async def cancel_trim_preorders(ex, st, sym, pos_side):
    """포지션 청산 시 해당 심볼의 trim 선주문 전량 취소.
    
    ★ V10.31c: runner.py에서 이동. 주문 실행 책임을 order_router에 통합.
    """
    from v9.execution.position_book import get_p
    import asyncio

    sym_st = st.get(sym, {})
    p = get_p(sym_st, pos_side)
    if not isinstance(p, dict):
        return

    trim_orders = p.get("trim_preorders", {})
    if not trim_orders:
        return

    for tier, info in list(trim_orders.items()):
        oid = info.get("oid", "")
        if not oid:
            continue
        try:
            await asyncio.to_thread(ex.cancel_order, oid, sym)
            remove_pending_limit(oid)
            print(f"[TRIM_CANCEL] {sym} {pos_side} T{tier} oid={oid} 취소")
        except Exception as e:
            # 이미 체결/취소된 경우 무시
            remove_pending_limit(oid)
            print(f"[TRIM_CANCEL] {sym} T{tier} 취소 시도: {e}")

    p["trim_preorders"] = {}
