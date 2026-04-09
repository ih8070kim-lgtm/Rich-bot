"""
V9 Beta Cycle Engine  (v10.29c — 1h 전환)
============================================
백테스트 결과: 1h_r24b168 +$921 WR72% PF1.48 MDD-5.8%

1h ohlcv_pool 기반. 일봉 fetch 제거.
bc_check_signals(): 5분마다 ARM/NORM 체크
bc_on_tick(): 매 틱 포지션 관리
"""
import time, uuid
import numpy as np
from typing import List, Dict, Optional, Tuple
from v9.types import Intent, IntentType
from v9.execution.position_book import get_p, iter_positions, ensure_slot
import v9.config as CFG

_armed: Dict[str, dict] = {}
_cooldown_until: Dict[str, float] = {}
_last_check_ts: float = 0.0
_exchange = None

def bc_init(exchange):
    global _exchange; _exchange = exchange

def _calc_excess_1h(sym, snapshot):
    pool = (snapshot.ohlcv_pool or {}).get(sym, {})
    ohlcv = pool.get("1h", [])
    btc_ohlcv = (snapshot.ohlcv_pool or {}).get("BTC/USDT", {}).get("1h", [])
    ret_w = getattr(CFG, 'BC_RETURN_WINDOW', 24)
    beta_w = getattr(CFG, 'BC_BETA_WINDOW', 168)
    need = beta_w + 5
    if len(ohlcv) < need or len(btc_ohlcv) < need: return None
    alt_c = [float(b[4]) for b in ohlcv[-need:]]
    btc_c = [float(b[4]) for b in btc_ohlcv[-need:]]
    if alt_c[-1] <= 0 or alt_c[-(ret_w+1)] <= 0 or btc_c[-1] <= 0 or btc_c[-(ret_w+1)] <= 0: return None
    alt_ret = (alt_c[-1] / alt_c[-(ret_w+1)]) - 1
    btc_ret = (btc_c[-1] / btc_c[-(ret_w+1)]) - 1
    try:
        alt_lr = np.diff(np.log(np.maximum(alt_c[-(beta_w+1):], 1e-10)))
        btc_lr = np.diff(np.log(np.maximum(btc_c[-(beta_w+1):], 1e-10)))
        n = min(len(alt_lr), len(btc_lr))
        if n < 20: return None
        var_b = np.var(btc_lr[-n:])
        if var_b < 1e-15: return None
        beta = float(np.cov(alt_lr[-n:], btc_lr[-n:])[0][1] / var_b)
    except: return None
    excess = alt_ret - (beta * btc_ret)
    return (excess, beta)

def _calc_atr_1h(sym, ohlcv):
    if len(ohlcv) < 12: return 0.01
    trs = []
    for i in range(-10, 0):
        h,l,pc = float(ohlcv[i][2]),float(ohlcv[i][3]),float(ohlcv[i-1][4])
        if pc > 0: trs.append(max(h-l, abs(h-pc), abs(l-pc)) / pc)
    return np.mean(trs) if trs else 0.01

def _count_bc(st):
    cnt = 0
    for sym, ss in st.items():
        if not isinstance(ss, dict): continue
        for side, p in iter_positions(ss):
            if isinstance(p, dict) and p.get("role") == "BC": cnt += 1
    return cnt

# ═══════════════════════════════════════════════════════════════
# bc_on_daily_close → bc_check_signals (하위 호환 유지)
# ═══════════════════════════════════════════════════════════════
def bc_on_daily_close(snapshot, st, system_state):
    """★ V10.29c: 이름 유지 (runner 호환), 내부는 1h 체크."""
    return bc_check_signals(snapshot, st, system_state)

def bc_check_signals(snapshot, st, system_state):
    global _last_check_ts
    if not getattr(CFG, 'BC_ENABLED', False): return []
    now = time.time()
    interval = getattr(CFG, 'BC_CHECK_INTERVAL', 300)
    if now - _last_check_ts < interval: return []
    _last_check_ts = now
    intents = []
    held = set()
    for sym, ss in st.items():
        if not isinstance(ss, dict): continue
        for side, p in iter_positions(ss):
            if p: held.add(sym)
    bc_count = _count_bc(st)
    pool_syms = set(snapshot.ohlcv_pool.keys()) if snapshot.ohlcv_pool else set()
    pool_syms.discard("BTC/USDT")
    for sym in pool_syms:
        if sym in held: continue
        if bc_count >= CFG.BC_MAX_POS: break
        if _cooldown_until.get(sym, 0) > now: continue
        er = _calc_excess_1h(sym, snapshot)
        if not er: continue
        excess, beta = er
        arm_t = getattr(CFG, 'BC_ARM_THRESH', 0.03)
        norm_t = getattr(CFG, 'BC_NORM_THRESH', 0.02)
        if excess >= arm_t:
            cur_p = float((snapshot.all_prices or {}).get(sym, 0))
            if sym not in _armed:
                _armed[sym] = {"ts": now, "peak_excess": excess, "peak_price": cur_p, "beta": beta}
                print(f"[BC] 🔔 ARMED {sym} excess={excess:+.1%} β={beta:.2f}")
                try:
                    from v9.logging.logger_csv import log_system
                    log_system("BC_ARM", f"{sym} excess={excess:+.1%} β={beta:.2f}")
                except: pass
            else:
                if excess > _armed[sym]["peak_excess"]: _armed[sym]["peak_excess"] = excess
                if cur_p > _armed[sym].get("peak_price", 0): _armed[sym]["peak_price"] = cur_p
        if sym in _armed and excess <= norm_t:
            arm = _armed.pop(sym)
            cur_p = float((snapshot.all_prices or {}).get(sym, 0))
            if cur_p <= 0 or bc_count >= CFG.BC_MAX_POS: continue
            equity = float(getattr(snapshot, "real_balance_usdt", 0) or 0)
            size = equity / CFG.BC_SIZE_DIVISOR
            qty = size / cur_p if cur_p > 0 and size >= 10 else 0
            if qty <= 0: continue
            intents.append(Intent(
                trace_id=str(uuid.uuid4())[:8], intent_type=IntentType.OPEN,
                symbol=sym, side="sell", qty=qty, price=cur_p,
                reason=f"BC_SHORT(ex={arm['peak_excess']:+.1%}→{excess:+.1%},β={arm['beta']:.2f})",
                metadata={"positionSide": "SHORT", "role": "BC",
                          "bc_peak_excess": arm["peak_excess"], "bc_entry_ts": now}))
            _cooldown_until[sym] = now + getattr(CFG, 'BC_COOLDOWN_SEC', 10800)
            bc_count += 1
            print(f"[BC] 📉 SHORT {sym} ex={arm['peak_excess']:+.1%}→{excess:+.1%} β={arm['beta']:.2f}")
            try:
                from v9.logging.logger_csv import log_system
                log_system("BC_SHORT", f"{sym} ex={arm['peak_excess']:+.1%}→{excess:+.1%}")
            except: pass
    expiry = getattr(CFG, 'BC_ARMED_EXPIRY_H', 168) * 3600
    expired = [s for s, a in _armed.items() if now - a["ts"] > expiry]
    for s in expired: del _armed[s]
    return intents

def bc_on_tick(snapshot, st):
    if not getattr(CFG, 'BC_ENABLED', False): return []
    intents = []
    for sym, sym_st in st.items():
        p = get_p(sym_st, "sell")
        if not p or not isinstance(p, dict) or p.get("role") != "BC": continue
        price = float((snapshot.all_prices or {}).get(sym, 0))
        ep = float(p.get("ep", 0))
        if price <= 0 or ep <= 0: continue
        hold_h = (time.time() - float(p.get("time", p.get("bc_entry_ts", time.time())))) / 3600
        roi = (ep - price) / ep
        ohlcv = (snapshot.ohlcv_pool or {}).get(sym, {}).get('1h', [])
        h_1h = float(ohlcv[-2][2]) if ohlcv and len(ohlcv) >= 2 else price
        l_1h = float(ohlcv[-2][3]) if ohlcv and len(ohlcv) >= 2 else price
        atr_pct = _calc_atr_1h(sym, ohlcv)
        trail_offset = max(CFG.BC_TRAIL_FLOOR, atr_pct * CFG.BC_TRAIL_ATR_MULT)
        trail_low = float(p.get("bc_trail_low", ep))
        if price < trail_low: trail_low = price; p["bc_trail_low"] = trail_low
        if l_1h < trail_low: trail_low = l_1h; p["bc_trail_low"] = trail_low
        trail_active = p.get("bc_trail_active", False)
        if not trail_active and roi >= CFG.BC_TRAIL_ACTIVATION:
            p["bc_trail_active"] = True; trail_active = True
        reason = None
        sl_p = ep * (1 + CFG.BC_SHORT_SL / 100)
        if price >= sl_p or h_1h >= sl_p: reason = "BC_SL"
        elif price <= ep * (1 - CFG.BC_SHORT_TP / 100): reason = "BC_TP"
        elif trail_active:
            ts = trail_low * (1 + trail_offset)
            if price >= ts or h_1h >= ts: reason = "BC_TRAIL"
        elif hold_h >= CFG.BC_MAX_HOLD_HOURS: reason = "BC_TIMEOUT"
        if reason:
            qty = float(p.get("amt", 0))
            if qty <= 0: continue
            intents.append(Intent(
                trace_id=str(uuid.uuid4())[:8], intent_type=IntentType.FORCE_CLOSE,
                symbol=sym, side="buy", qty=qty, price=price,
                reason=f"{reason}(roi={roi:+.1%},hold={hold_h:.0f}h)",
                metadata={"positionSide": "SHORT", "role": "BC"}))
            _cooldown_until[sym] = time.time() + getattr(CFG, 'BC_COOLDOWN_SEC', 10800)
            print(f"[BC] {reason} {sym} roi={roi:+.1%} hold={hold_h:.0f}h")
            try:
                from v9.logging.logger_csv import log_system
                log_system("BC_EXIT", f"{sym} {reason} roi={roi:+.1%}")
            except: pass
    return intents

def bc_save_state(system_state):
    system_state["_bc_armed"] = dict(_armed)
    system_state["_bc_cooldown_until"] = dict(_cooldown_until)

def bc_restore_state(system_state):
    global _armed, _cooldown_until
    _armed = system_state.get("_bc_armed", {})
    _cooldown_until = system_state.get("_bc_cooldown_until", {})
    print(f"[BC_RESTORE] armed={len(_armed)} cd={len(_cooldown_until)}")
    try:
        from v9.logging.logger_csv import log_system
        log_system("BC_RESTORE", f"armed={len(_armed)} cd={len(_cooldown_until)}")
    except: pass
