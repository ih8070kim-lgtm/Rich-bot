"""
V9 Beta Cycle Engine  (v10.29b-BC)
====================================
MR 파이프라인에 통합되는 Beta Cycle 숏 전용 엔진.
Intent 기반: OPEN(숏 진입) / FORCE_CLOSE(청산) 생성 → 기존 risk/execute 경유.

호출 지점 (runner.py):
  1) bc_on_daily_close()  — UTC 00:00 일봉 마감 시 1회 (시그널 판단)
  2) bc_on_tick()          — 매 틱 (포지션 관리: SL/TP/Trail/Timeout)

데이터:
  - 1d bars: 자체 fetch (ccxt, 1일 1회)
  - 1h bars: snapshot.ohlcv_pool 재사용

MR 포지션과 분리:
  - role="BC" 태그로 구분
  - MR planners는 _HEDGE_ROLES_SLOT에 "BC" 포함 → 자동 스킵
  - slot_manager는 BC 포지션을 MR 슬롯에서 제외
"""
import time
import uuid
import numpy as np
from collections import deque
from typing import List, Dict, Optional, Tuple

from v9.types import Intent, IntentType
from v9.execution.position_book import get_p, iter_positions, ensure_slot

import v9.config as CFG

# ═══════════════════════════════════════════════════════════════
# State (모듈 레벨 — 프로세스 수명 동안 유지)
# ═══════════════════════════════════════════════════════════════
_daily_closes: Dict[str, deque] = {}      # {sym: deque(maxlen=90)}
_daily_volumes: Dict[str, deque] = {}
_btc_daily: deque = deque(maxlen=90)
_armed: Dict[str, dict] = {}              # {sym: {ts, peak_excess, peak_price, beta_7d}}
_cooldown_until: Dict[str, float] = {}    # {sym: unix_ts}
_daily_entry_count: int = 0
_last_entry_date: str = ""
_universe: set = set()
_last_daily_fetch_date: str = ""
_last_1h_ts: Dict[str, float] = {}       # 1h 봉 변경 감지용

# 일봉 데이터 fetcher를 위한 exchange 참조
_exchange = None


def bc_init(exchange):
    """runner.py 초기화 시 호출 — exchange 객체 전달."""
    global _exchange
    _exchange = exchange


# ═══════════════════════════════════════════════════════════════
# 일봉 마감 시 호출 (시그널 판단)
# ═══════════════════════════════════════════════════════════════
def bc_on_daily_close(snapshot, st: Dict, system_state: Dict) -> List[Intent]:
    """UTC 00:00 일봉 마감 시 1회 호출.
    Returns: OPEN Intent 리스트 (숏 진입)
    """
    global _daily_entry_count, _last_entry_date

    if not getattr(CFG, 'BC_ENABLED', False):
        return []

    today = time.strftime("%Y-%m-%d", time.gmtime())
    if today != _last_entry_date:
        _daily_entry_count = 0
        _last_entry_date = today

    # 일봉 데이터 갱신 (1일 1회)
    _fetch_daily_bars_if_needed()
    _update_universe()

    if len(_btc_daily) < CFG.BC_BETA_LONG_D + 8:
        return []

    intents: List[Intent] = []

    # 현재 BC + MR 보유 심볼
    held_syms = set()
    for sym, sym_st in st.items():
        for side, p in iter_positions(sym_st):
            if p:
                held_syms.add(sym)

    bc_count = _count_bc_positions(st)

    for sym in _universe:
        if sym in held_syms:
            continue
        if _daily_entry_count >= CFG.BC_ENTRY_PER_DAY:
            break
        if bc_count >= CFG.BC_MAX_POS:
            break

        # 쿨다운
        if sym in _cooldown_until and time.time() < _cooldown_until[sym]:
            continue

        er = _calc_excess(sym)
        if not er:
            continue
        excess, beta_7d = er
        vol_surge = _calc_vol_surge(sym)

        # ── ARMED ──
        if excess >= CFG.BC_ARM_THRESH and vol_surge >= 1.0:
            cur_p = _daily_closes[sym][-1] if sym in _daily_closes else 0
            if sym not in _armed:
                _armed[sym] = {
                    "ts": time.time(),
                    "peak_excess": excess,
                    "peak_price": cur_p,
                    "beta_7d": beta_7d,
                }
                print(f"[BC] 🔔 ARMED {sym} excess={excess:+.1%} β7d={beta_7d:.2f} vol={vol_surge:.1f}x")
            else:
                if excess > _armed[sym]["peak_excess"]:
                    _armed[sym]["peak_excess"] = excess
                if cur_p > _armed[sym]["peak_price"]:
                    _armed[sym]["peak_price"] = cur_p

        # ── SHORT 진입 ──
        if sym in _armed and excess <= CFG.BC_NORM_THRESH:
            arm = _armed[sym]
            cur_p = _daily_closes[sym][-1] if sym in _daily_closes and _daily_closes[sym] else 0
            if cur_p <= 0:
                continue

            pullback = (arm["peak_price"] - cur_p) / arm["peak_price"] if arm["peak_price"] > 0 else 0
            if pullback > CFG.BC_PULLBACK_MAX:
                print(f"[BC] ⏭ SKIP {sym} pullback={pullback:.1%} > max")
                _armed.pop(sym, None)
                continue
            if pullback < CFG.BC_PULLBACK_MIN:
                continue

            # 사이즈
            equity = snapshot.real_balance_usdt
            if equity <= 0:
                continue
            notional = equity / CFG.BC_SIZE_DIVISOR
            price = snapshot.all_prices.get(sym, cur_p)
            if price <= 0:
                continue
            qty = notional / price

            # 최소 수량 체크
            min_qty = CFG.SYM_MIN_QTY.get(sym, CFG.SYM_MIN_QTY_DEFAULT)
            if qty < min_qty:
                continue
            if notional < 10:
                continue

            # 슬롯 확보
            ensure_slot(st, sym)

            intent = Intent(
                trace_id=f"BC_{uuid.uuid4().hex[:8]}",
                intent_type=IntentType.OPEN,
                symbol=sym,
                side="sell",
                qty=qty,
                price=None,
                reason=f"BC_SHORT ex={arm['peak_excess']:+.1%}→{excess:+.1%} pb={pullback:.1%}",
                metadata={
                    "positionSide": "SHORT",
                    "role": "BC",
                    "bc_peak_excess": arm["peak_excess"],
                    "bc_beta_7d": arm["beta_7d"],
                    "bc_entry_ts": time.time(),
                    "bc_trail_low": price,
                    "bc_trail_active": False,
                },
            )
            intents.append(intent)
            _armed.pop(sym, None)
            _daily_entry_count += 1
            bc_count += 1
            held_syms.add(sym)

            print(f"[BC] 📉 SHORT {sym} ex_peak={arm['peak_excess']:+.1%}→{excess:+.1%} "
                  f"pb={pullback:.1%} β7d={arm['beta_7d']:.2f} "
                  f"qty={qty:.4f} notional=${notional:.0f} [{_daily_entry_count}/{CFG.BC_ENTRY_PER_DAY}]")

        # ARMED 만료
        if sym in _armed:
            age_days = (time.time() - _armed[sym]["ts"]) / 86400
            if age_days > CFG.BC_ARMED_EXPIRY_D:
                _armed.pop(sym, None)

    return intents


# ═══════════════════════════════════════════════════════════════
# 매 틱 호출 (포지션 관리)
# ═══════════════════════════════════════════════════════════════
def bc_on_tick(snapshot, st: Dict) -> List[Intent]:
    """매 틱에서 BC 포지션 SL/TP/Trail/Timeout 관리.
    Returns: FORCE_CLOSE Intent 리스트
    """
    if not getattr(CFG, 'BC_ENABLED', False):
        return []

    intents: List[Intent] = []

    for sym, sym_st in st.items():
        p = get_p(sym_st, "sell")
        if not p or not isinstance(p, dict):
            continue
        if p.get("role") != "BC":
            continue

        price = snapshot.all_prices.get(sym, 0)
        if price <= 0:
            continue

        ep = p.get("ep", 0)
        if ep <= 0:
            continue

        entry_ts = p.get("time", p.get("bc_entry_ts", time.time()))
        hold_hours = (time.time() - entry_ts) / 3600
        roi = (ep - price) / ep  # 숏이므로 양수=수익

        # ── 1h 봉 변경 감지 (ohlcv_pool에서) ──
        ohlcv = snapshot.ohlcv_pool.get(sym, {}).get('1h', [])

        # 1h high/low (현재 봉)
        if ohlcv and len(ohlcv) >= 2:
            cur_bar = ohlcv[-2]  # 마감된 직전 1h 봉
            h_1h = float(cur_bar[2])
            l_1h = float(cur_bar[3])
        else:
            h_1h = price
            l_1h = price

        # ATR 기반 트레일 오프셋
        atr_pct = _calc_atr_1h(sym, ohlcv)
        trail_offset = max(CFG.BC_TRAIL_FLOOR, atr_pct * CFG.BC_TRAIL_ATR_MULT)

        # 트레일 저점 갱신
        trail_low = p.get("bc_trail_low", ep)
        if price < trail_low:
            trail_low = price
            p["bc_trail_low"] = trail_low
        if l_1h < trail_low:
            trail_low = l_1h
            p["bc_trail_low"] = trail_low

        # 트레일 활성화
        trail_active = p.get("bc_trail_active", False)
        if not trail_active and roi >= CFG.BC_TRAIL_ACTIVATION:
            p["bc_trail_active"] = True
            trail_active = True

        # ── 청산 판단 ──
        reason = None

        # SL (가격 기준)
        sl_price = ep * (1 + CFG.BC_SHORT_SL / 100)
        if price >= sl_price or h_1h >= sl_price:
            reason = "BC_SL"

        # TP
        elif price <= ep * (1 - CFG.BC_SHORT_TP / 100):
            reason = "BC_TP"

        # TRAIL
        elif trail_active:
            trail_stop = trail_low * (1 + trail_offset)
            if price >= trail_stop or h_1h >= trail_stop:
                reason = "BC_TRAIL"

        # TIMEOUT
        elif hold_hours >= CFG.BC_MAX_HOLD_HOURS:
            reason = "BC_TIMEOUT"

        if reason:
            amt = p.get("amt", 0)
            if amt <= 0:
                continue

            intent = Intent(
                trace_id=f"BC_{uuid.uuid4().hex[:8]}",
                intent_type=IntentType.FORCE_CLOSE,
                symbol=sym,
                side="buy",  # 숏 청산 = 매수
                qty=amt,
                price=None,
                reason=f"{reason} roi={roi:+.1%} hold={hold_hours:.0f}h tr_off={trail_offset:.1%}",
                metadata={
                    "positionSide": "SHORT",
                    "role": "BC",
                    "_expected_role": "BC",
                },
            )
            intents.append(intent)
            _cooldown_until[sym] = time.time() + CFG.BC_COOLDOWN_DAYS * 86400
            print(f"[BC] {'✅' if roi > 0 else '❌'} {reason} {sym} "
                  f"roi={roi:+.1%} hold={hold_hours:.0f}h")

    return intents


# ═══════════════════════════════════════════════════════════════
# 내부 함수
# ═══════════════════════════════════════════════════════════════

def _count_bc_positions(st: Dict) -> int:
    count = 0
    for sym, sym_st in st.items():
        p = get_p(sym_st, "sell")
        if p and isinstance(p, dict) and p.get("role") == "BC":
            count += 1
    return count


def _fetch_daily_bars_if_needed():
    """1일 1회 일봉 데이터 갱신."""
    global _last_daily_fetch_date
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if today == _last_daily_fetch_date:
        return
    if _exchange is None:
        return

    _last_daily_fetch_date = today
    print(f"[BC] 📥 Fetching daily bars...")

    # BTC
    try:
        btc_bars = _exchange.fetch_ohlcv("BTC/USDT", "1d", limit=90)
        _btc_daily.clear()
        for b in btc_bars:
            _btc_daily.append(float(b[4]))  # close
    except Exception as e:
        print(f"[BC] BTC 1d fetch 실패: {e}")
        return

    # 후보 심볼
    for sym in getattr(CFG, 'BC_CANDIDATE_POOL', _DEFAULT_POOL):
        try:
            bars = _exchange.fetch_ohlcv(sym, "1d", limit=90)
            if sym not in _daily_closes:
                _daily_closes[sym] = deque(maxlen=90)
                _daily_volumes[sym] = deque(maxlen=90)
            _daily_closes[sym].clear()
            _daily_volumes[sym].clear()
            for b in bars:
                _daily_closes[sym].append(float(b[4]))
                _daily_volumes[sym].append(float(b[5]))
        except Exception as e:
            print(f"[BC] {sym} 1d fetch 실패(무시): {e}")
        time.sleep(0.05)

    print(f"[BC] ✅ Daily bars: BTC({len(_btc_daily)}) + {len(_daily_closes)} alts")


def _update_universe():
    """excess_vol + mr_tendency 스코어링 → 상위 N개."""
    global _universe

    if len(_btc_daily) < CFG.BC_BETA_LONG_D + 65:
        return

    scores = []
    btc_c = list(_btc_daily)

    for sym, dc in _daily_closes.items():
        c = list(dc)
        v = list(_daily_volumes.get(sym, []))
        if len(c) < CFG.BC_BETA_LONG_D + 65:
            continue

        # 일평균 거래대금
        if len(c) >= 30 and len(v) >= 30:
            notionals = [p * vol for p, vol in zip(c[-30:], v[-30:]) if p > 0 and vol > 0]
            avg_vol = np.mean(notionals) if notionals else 0
            if avg_vol < 30_000_000:  # $30M 최소
                continue

        # excess return 시계열 (60일)
        excess_hist = []
        for di in range(max(0, len(c) - 60), len(c)):
            if di < CFG.BC_RETURN_WINDOW + CFG.BC_BETA_LONG_D + 1:
                continue
            if di < len(c) and di < len(btc_c):
                try:
                    alt_ret = (c[di] / c[di - CFG.BC_RETURN_WINDOW]) - 1
                    btc_ret = (btc_c[di] / btc_c[di - CFG.BC_RETURN_WINDOW]) - 1
                    # beta_30d (간이)
                    alt_lr = np.diff(np.log(c[di - CFG.BC_BETA_LONG_D:di + 1]))
                    btc_lr = np.diff(np.log(btc_c[di - CFG.BC_BETA_LONG_D:di + 1]))
                    n = min(len(alt_lr), len(btc_lr))
                    if n >= 10:
                        var_b = np.var(btc_lr[-n:])
                        b30 = float(np.cov(alt_lr[-n:], btc_lr[-n:])[0][1] / var_b) if var_b > 1e-15 else 1.0
                        excess = alt_ret - (b30 * btc_ret)
                        excess_hist.append(excess)
                except Exception:
                    pass

        if len(excess_hist) < 20:
            continue

        ev = float(np.std(excess_hist))
        ea = np.array(excess_hist)
        lag = min(5, len(ea) // 3)
        try:
            mr = -float(np.corrcoef(ea[:-lag], ea[lag:])[0][1]) if len(ea) > lag * 2 else 0.0
        except Exception:
            mr = 0.0

        if np.isnan(ev) or np.isnan(mr):
            continue
        scores.append((sym, ev * 0.6 + max(0, mr) * 0.4))

    scores.sort(key=lambda x: -x[1])
    _universe = {s[0] for s in scores[:CFG.BC_UNI_TOP_N]}


def _calc_excess(sym) -> Optional[Tuple[float, float]]:
    """일봉 버퍼에서 excess return + beta_7d 계산."""
    if sym not in _daily_closes or len(_daily_closes[sym]) < CFG.BC_BETA_LONG_D + 5:
        return None
    if len(_btc_daily) < CFG.BC_BETA_LONG_D + 5:
        return None

    ac = list(_daily_closes[sym])
    bc = list(_btc_daily)
    w = CFG.BC_RETURN_WINDOW

    if len(ac) < w + 1 or len(bc) < w + 1:
        return None
    if ac[-1] <= 0 or ac[-(w + 1)] <= 0 or bc[-1] <= 0 or bc[-(w + 1)] <= 0:
        return None

    alt_ret = (ac[-1] / ac[-(w + 1)]) - 1
    btc_ret = (bc[-1] / bc[-(w + 1)]) - 1

    # beta
    try:
        alt_lr = np.diff(np.log(ac[-(CFG.BC_BETA_LONG_D + 1):]))
        btc_lr = np.diff(np.log(bc[-(CFG.BC_BETA_LONG_D + 1):]))
        n = min(len(alt_lr), len(btc_lr))
        if n < 10:
            return None
        var_b = np.var(btc_lr[-n:])
        beta_30d = float(np.cov(alt_lr[-n:], btc_lr[-n:])[0][1] / var_b) if var_b > 1e-15 else 1.0
        beta_7d = float(np.cov(alt_lr[-w:], btc_lr[-w:])[0][1] / var_b) if var_b > 1e-15 else 1.0
    except Exception:
        return None

    excess = alt_ret - (beta_30d * btc_ret)
    return (excess, beta_7d)


def _calc_vol_surge(sym) -> float:
    if sym not in _daily_volumes:
        return 1.0
    v = list(_daily_volumes[sym])
    if len(v) < 30:
        return 1.0
    v_short = np.mean(v[-7:])
    v_long = np.mean(v[-30:])
    return v_short / v_long if v_long > 0 else 1.0


def _calc_atr_1h(sym, ohlcv_1h) -> float:
    """1h ohlcv에서 ATR % 계산."""
    if not ohlcv_1h or len(ohlcv_1h) < 26:
        return 0.02  # 기본값

    trs = []
    for i in range(-25, -1):
        try:
            h = float(ohlcv_1h[i][2])
            l = float(ohlcv_1h[i][3])
            c_prev = float(ohlcv_1h[i - 1][4])
            if c_prev <= 0:
                continue
            tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
            trs.append(tr / c_prev)
        except (IndexError, TypeError, ValueError):
            continue

    return float(np.mean(trs)) if trs else 0.02


# 기본 후보 풀 (config에 BC_CANDIDATE_POOL이 없을 때)
# ★ V10.29c: GLOBAL_BLACKLIST 심볼 제거 (DOGE, WIF)
_DEFAULT_POOL = sorted({
    "XRP/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT",
    "ICP/USDT", "ETC/USDT", "XLM/USDT", "ARB/USDT", "OP/USDT",
    "SEI/USDT", "INJ/USDT", "WLD/USDT", "TIA/USDT", "GRT/USDT",
    "STRK/USDT", "SUI/USDT", "NEAR/USDT", "AAVE/USDT", "UNI/USDT",
    "APT/USDT", "ATOM/USDT", "STX/USDT", "FET/USDT", "FIL/USDT",
    "RUNE/USDT", "JUP/USDT", "PENDLE/USDT",
    "ORDI/USDT", "PYTH/USDT", "MANTA/USDT", "DYM/USDT",
    "JASMY/USDT", "1000SATS/USDT", "NOT/USDT",
})


# ═══════════════════════════════════════════════════════════════
# ★ V10.29c: State 영속화 (재시작 시 ARMED/쿨다운 보존)
# ═══════════════════════════════════════════════════════════════
def bc_save_state(system_state: dict):
    """모듈 글로벌 → system_state."""
    system_state["_bc_armed"] = dict(_armed)
    system_state["_bc_cooldown_until"] = dict(_cooldown_until)
    system_state["_bc_daily_entry_count"] = _daily_entry_count
    system_state["_bc_last_entry_date"] = _last_entry_date


def bc_restore_state(system_state: dict):
    """system_state → 모듈 글로벌."""
    global _armed, _cooldown_until, _daily_entry_count, _last_entry_date
    _armed = system_state.get("_bc_armed", {})
    _cooldown_until = system_state.get("_bc_cooldown_until", {})
    _daily_entry_count = system_state.get("_bc_daily_entry_count", 0)
    _last_entry_date = system_state.get("_bc_last_entry_date", "")
    if _armed:
        print(f"[BC_RESTORE] armed={list(_armed.keys())} cd={len(_cooldown_until)}")
    else:
        print(f"[BC_RESTORE] armed=0 cd={len(_cooldown_until)}")
