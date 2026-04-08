"""
V9 Crash Bounce Engine  (v10.29b-CB)
======================================
BTC 급락 감지 → 고베타 알트 롱. BC의 거울상.

Best config (WF 검증):
  CR5% | 24h8% | VS1.0 | DL0h | SL3% | TR_ATR1.0 | MH48h | B3
  WR=72% | PF=3.71 | MDD=-2.1% | 12mo OOS $+211

호출: cb_on_tick() — 매 틱
  1) BTC 1h ohlcv로 4h/24h ROC 계산
  2) 크래시 감지 → 고베타 알트 롱
  3) 보유 중 SL/Trail/Timeout 관리

데이터:
  - BTC 1h: snapshot.ohlcv_pool (이미 수집됨)
  - 알트 일봉: 자체 fetch (1일 1회, beta 계산용)
"""
import time
import uuid
import numpy as np
from collections import deque
from typing import List, Dict

from v9.types import Intent, IntentType
from v9.execution.position_book import get_p, iter_positions, ensure_slot

import v9.config as CFG

# ═══════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════
_exchange = None
_daily_closes: Dict[str, deque] = {}
_btc_daily: deque = deque(maxlen=60)
_last_daily_fetch: str = ""

# 크래시 상태
_last_crash_ts: float = 0.0
_crash_active: bool = False
_crash_trigger_ts: float = 0.0
_crash_entries: int = 0
_crash_roc: float = 0.0

# 기본 후보 풀
# ★ V10.29c: GLOBAL_BLACKLIST 심볼 제거 (DOGE, WIF)
_DEFAULT_POOL = sorted({
    "XRP/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT",
    "ICP/USDT", "ETC/USDT", "XLM/USDT", "ARB/USDT", "OP/USDT",
    "SEI/USDT", "INJ/USDT", "WLD/USDT", "TIA/USDT", "GRT/USDT",
    "STRK/USDT", "SUI/USDT", "NEAR/USDT", "AAVE/USDT", "UNI/USDT",
    "APT/USDT", "ATOM/USDT", "STX/USDT", "FET/USDT", "FIL/USDT",
    "RUNE/USDT", "JUP/USDT", "PENDLE/USDT",
    "ORDI/USDT", "MANTA/USDT", "DYM/USDT", "NOT/USDT",
})


def cb_init(exchange):
    global _exchange
    _exchange = exchange


# ═══════════════════════════════════════════════════════════════
# 매 틱 호출
# ═══════════════════════════════════════════════════════════════
def cb_on_tick(snapshot, st: Dict) -> List[Intent]:
    """BTC 크래시 감지 + CB 포지션 관리."""
    global _last_crash_ts, _crash_active, _crash_trigger_ts, _crash_entries, _crash_roc

    if not getattr(CFG, 'CB_ENABLED', False):
        return []

    intents: List[Intent] = []

    # ── 일봉 fetch (beta 계산, 1일 1회) ──
    _fetch_daily_if_needed()

    # ── BTC 1h ohlcv에서 ROC ──
    btc_ohlcv = snapshot.ohlcv_pool.get('BTC/USDT', {}).get('1h', [])
    if not btc_ohlcv or len(btc_ohlcv) < 26:
        return _manage_positions(snapshot, st)

    btc_c = [float(b[4]) for b in btc_ohlcv]
    btc_v = [float(b[5]) for b in btc_ohlcv]

    if btc_c[-1] <= 0:
        return _manage_positions(snapshot, st)

    # 4h ROC (직전 마감 봉 기준 — [-2] vs [-6])
    roc_4h = 0.0
    if len(btc_c) >= 7 and btc_c[-6] > 0:
        roc_4h = (btc_c[-2] / btc_c[-6]) - 1

    # 24h ROC
    roc_24h = 0.0
    if len(btc_c) >= 26 and btc_c[-26] > 0:
        roc_24h = (btc_c[-2] / btc_c[-26]) - 1

    # BTC 볼륨 서지 (4봉/24봉)
    vol_surge = 1.0
    if len(btc_v) >= 26:
        v_fast = np.mean(btc_v[-5:-1]) if len(btc_v) >= 5 else 0
        v_slow = np.mean(btc_v[-25:-1]) if len(btc_v) >= 25 else 1
        vol_surge = v_fast / v_slow if v_slow > 0 else 1.0

    now = time.time()

    # ── 크래시 감지 ──
    if not _crash_active:
        triggered = (roc_4h <= CFG.CB_CRASH_4H and vol_surge >= CFG.CB_VOL_SURGE_GATE) or \
                    (roc_24h <= CFG.CB_CRASH_24H)

        if triggered and (now - _last_crash_ts) >= CFG.CB_COOLDOWN_H * 3600:
            _crash_active = True
            _crash_trigger_ts = now
            _crash_entries = 0
            _crash_roc = min(roc_4h, roc_24h)
            print(f"[CB] 🚨 CRASH BTC 4h={roc_4h:+.1%} 24h={roc_24h:+.1%} vol={vol_surge:.1f}x")

    # ── 크래시 활성 → 롱 진입 ──
    if _crash_active:
        delay_ok = (now - _crash_trigger_ts) >= CFG.CB_ENTRY_DELAY_H * 3600
        cb_count = _count_cb_positions(st)

        if delay_ok and _crash_entries < CFG.CB_MAX_ENTRIES and cb_count < CFG.CB_MAX_POS:
            # 보유 심볼 체크
            held = set()
            for sym, sym_st in st.items():
                for side, p in iter_positions(sym_st):
                    if p: held.add(sym)

            # beta 랭킹
            beta_rank = _rank_by_beta(CFG.CB_TOP_BETA_N)

            for sym, beta in beta_rank:
                if sym in held: continue
                if _crash_entries >= CFG.CB_MAX_ENTRIES: break
                if cb_count >= CFG.CB_MAX_POS: break

                price = snapshot.all_prices.get(sym, 0)
                if price <= 0: continue

                equity = snapshot.real_balance_usdt
                if equity <= 0: continue
                notional = equity * CFG.CB_SIZE_PCT
                qty = notional / price
                if qty <= 0 or notional < 20: continue

                # 최소 수량
                min_qty = CFG.SYM_MIN_QTY.get(
                    sym.replace("/USDT", "USDT"), CFG.SYM_MIN_QTY_DEFAULT)
                if qty < min_qty: continue

                ensure_slot(st, sym)
                fill_p = price * 1.0005  # 슬리피지

                intent = Intent(
                    trace_id=f"CB_{uuid.uuid4().hex[:8]}",
                    intent_type=IntentType.OPEN,
                    symbol=sym,
                    side="buy",
                    qty=qty,
                    price=None,
                    reason=f"CB_LONG crash={_crash_roc:+.1%} β={beta:.2f}",
                    metadata={
                        "positionSide": "LONG",
                        "role": "CB",
                        "cb_beta": beta,
                        "cb_crash_roc": _crash_roc,
                        "cb_entry_ts": now,
                        "cb_trail_high": fill_p,
                        "cb_trail_active": False,
                    },
                )
                intents.append(intent)
                _crash_entries += 1
                cb_count += 1
                held.add(sym)

                print(f"[CB] 📈 LONG {sym} @{price:.4f} β={beta:.2f} "
                      f"notional=${notional:.0f} [{_crash_entries}/{CFG.CB_MAX_ENTRIES}]")

        # 진입 완료 or 4봉(4시간) 지나면 이벤트 종료
        if _crash_entries >= CFG.CB_MAX_ENTRIES or \
           (now - _crash_trigger_ts) > (CFG.CB_ENTRY_DELAY_H + 4) * 3600:
            _crash_active = False
            _last_crash_ts = _crash_trigger_ts

    # ── 포지션 관리 ──
    intents += _manage_positions(snapshot, st)

    return intents


# ═══════════════════════════════════════════════════════════════
# 포지션 관리 (SL / Trail / Timeout)
# ═══════════════════════════════════════════════════════════════
def _manage_positions(snapshot, st: Dict) -> List[Intent]:
    intents: List[Intent] = []

    for sym, sym_st in st.items():
        p = get_p(sym_st, "buy")
        if not p or not isinstance(p, dict): continue
        if p.get("role") != "CB": continue

        price = snapshot.all_prices.get(sym, 0)
        if price <= 0: continue

        ep = p.get("ep", 0)
        if ep <= 0: continue

        entry_ts = p.get("time", p.get("cb_entry_ts", time.time()))
        hold_hours = (time.time() - entry_ts) / 3600
        roi = (price - ep) / ep  # 롱이므로 양수=수익

        # 1h ohlcv에서 ATR
        ohlcv = snapshot.ohlcv_pool.get(sym, {}).get('1h', [])
        atr_pct = _calc_atr_1h(ohlcv)
        trail_offset = max(CFG.CB_TRAIL_FLOOR, atr_pct * CFG.CB_TRAIL_ATR_MULT)

        # 고점 추적
        trail_high = p.get("cb_trail_high", ep)
        if price > trail_high:
            trail_high = price
            p["cb_trail_high"] = trail_high

        # 트레일 활성화
        trail_active = p.get("cb_trail_active", False)
        if not trail_active and roi >= CFG.CB_TRAIL_ACTIVATION:
            p["cb_trail_active"] = True
            trail_active = True

        # 청산 판단
        reason = None

        # SL (가격 하락)
        sl_price = ep * (1 - CFG.CB_SL_PCT / 100)
        # 1h low 체크
        if ohlcv and len(ohlcv) >= 2:
            low_1h = float(ohlcv[-2][3])
            if low_1h <= sl_price or price <= sl_price:
                reason = "CB_SL"
        elif price <= sl_price:
            reason = "CB_SL"

        # TRAIL
        if not reason and trail_active:
            trail_stop = trail_high * (1 - trail_offset)
            if price <= trail_stop:
                reason = "CB_TRAIL"

        # TIMEOUT
        if not reason and hold_hours >= CFG.CB_MAX_HOLD_H:
            reason = "CB_TIMEOUT"

        if reason:
            amt = p.get("amt", 0)
            if amt <= 0: continue

            intent = Intent(
                trace_id=f"CB_{uuid.uuid4().hex[:8]}",
                intent_type=IntentType.FORCE_CLOSE,
                symbol=sym,
                side="sell",  # 롱 청산 = 매도
                qty=amt,
                price=None,
                reason=f"{reason} roi={roi:+.1%} hold={hold_hours:.0f}h",
                metadata={
                    "positionSide": "LONG",
                    "role": "CB",
                    "_expected_role": "CB",
                },
            )
            intents.append(intent)
            print(f"[CB] {'✅' if roi > 0 else '❌'} {reason} {sym} "
                  f"roi={roi:+.1%} hold={hold_hours:.0f}h")

    return intents


# ═══════════════════════════════════════════════════════════════
# Beta 랭킹
# ═══════════════════════════════════════════════════════════════
def _rank_by_beta(top_n=3):
    """일봉 버퍼 기준 beta 상위 심볼."""
    if len(_btc_daily) < 35:
        return []

    btc_c = list(_btc_daily)
    betas = []

    for sym, dc in _daily_closes.items():
        c = list(dc)
        if len(c) < 35: continue

        try:
            alt_lr = np.diff(np.log(c[-31:]))
            btc_lr = np.diff(np.log(btc_c[-31:]))
            n = min(len(alt_lr), len(btc_lr))
            if n < 10: continue
            vb = np.var(btc_lr[-n:])
            if vb < 1e-15: continue
            beta = float(np.cov(alt_lr[-n:], btc_lr[-n:])[0][1] / vb)
            if beta > 0.5:
                betas.append((sym, beta))
        except Exception:
            continue

    betas.sort(key=lambda x: -x[1])
    return betas[:top_n]


# ═══════════════════════════════════════════════════════════════
# 일봉 fetch (1일 1회)
# ═══════════════════════════════════════════════════════════════
def _fetch_daily_if_needed():
    global _last_daily_fetch
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if today == _last_daily_fetch: return
    if _exchange is None: return

    _last_daily_fetch = today
    print(f"[CB] 📥 Fetching daily bars for beta...")

    try:
        btc_bars = _exchange.fetch_ohlcv("BTC/USDT", "1d", limit=60)
        _btc_daily.clear()
        for b in btc_bars:
            _btc_daily.append(float(b[4]))
    except Exception as e:
        print(f"[CB] BTC 1d fetch 실패: {e}")
        return

    pool = getattr(CFG, 'CB_CANDIDATE_POOL', _DEFAULT_POOL)
    for sym in pool:
        try:
            bars = _exchange.fetch_ohlcv(sym, "1d", limit=60)
            if sym not in _daily_closes:
                _daily_closes[sym] = deque(maxlen=60)
            _daily_closes[sym].clear()
            for b in bars:
                _daily_closes[sym].append(float(b[4]))
        except Exception:
            pass
        time.sleep(0.05)

    print(f"[CB] ✅ Daily: BTC({len(_btc_daily)}) + {len(_daily_closes)} alts")


def _count_cb_positions(st: Dict) -> int:
    count = 0
    for sym, sym_st in st.items():
        p = get_p(sym_st, "buy")
        if p and isinstance(p, dict) and p.get("role") == "CB":
            count += 1
    return count


def _calc_atr_1h(ohlcv_1h) -> float:
    if not ohlcv_1h or len(ohlcv_1h) < 16:
        return 0.02
    trs = []
    for i in range(-15, -1):
        try:
            h = float(ohlcv_1h[i][2])
            l = float(ohlcv_1h[i][3])
            c_prev = float(ohlcv_1h[i - 1][4])
            if c_prev <= 0: continue
            tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
            trs.append(tr / c_prev)
        except (IndexError, TypeError, ValueError):
            continue
    return float(np.mean(trs)) if trs else 0.02


# ═══════════════════════════════════════════════════════════════
# ★ V10.29c: State 영속화 (재시작 시 크래시 이벤트 상태 보존)
# ═══════════════════════════════════════════════════════════════
def cb_save_state(system_state: dict):
    """모듈 글로벌 → system_state."""
    system_state["_cb_last_crash_ts"] = _last_crash_ts
    system_state["_cb_crash_active"] = _crash_active
    system_state["_cb_crash_trigger_ts"] = _crash_trigger_ts
    system_state["_cb_crash_entries"] = _crash_entries
    system_state["_cb_crash_roc"] = _crash_roc


def cb_restore_state(system_state: dict):
    """system_state → 모듈 글로벌."""
    global _last_crash_ts, _crash_active, _crash_trigger_ts, _crash_entries, _crash_roc
    _last_crash_ts = system_state.get("_cb_last_crash_ts", 0.0)
    _crash_active = system_state.get("_cb_crash_active", False)
    _crash_trigger_ts = system_state.get("_cb_crash_trigger_ts", 0.0)
    _crash_entries = system_state.get("_cb_crash_entries", 0)
    _crash_roc = system_state.get("_cb_crash_roc", 0.0)
    if _crash_active:
        print(f"[CB_RESTORE] crash ACTIVE roc={_crash_roc:+.1%} entries={_crash_entries}")
    else:
        print(f"[CB_RESTORE] idle (last_crash={_last_crash_ts:.0f})")
