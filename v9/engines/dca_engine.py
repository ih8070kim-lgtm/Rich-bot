"""
★ V10.29e: plan_counter — BB Squeeze 브레이크아웃 진입.
planners.py에서 분리. role=CORE_MR → DCA/TP/SL 기존 MR과 동일.
"""
import time
import uuid
from typing import List, Dict

from v9.types import Intent, IntentType, MarketSnapshot
from v9.execution.position_book import iter_positions


# ═════════════════════════════════════════════════════════════════
# 모듈 상태 (영속화 대상)
# ═════════════════════════════════════════════════════════════════
_bb_cooldowns: Dict[str, float] = {}       # "SYM_side" → next allowed timestamp
_bb_squeeze_count: Dict[str, int] = {}     # SYM → 연속 스퀴즈 봉 수
_bb_prev_squeeze_len: Dict[str, int] = {}  # SYM → 직전 스퀴즈 길이
_bb_last_bar_ts: Dict[str, int] = {}       # SYM → 마지막 처리한 1h 봉 ts

# BB Squeeze 파라미터
_BB_P = 20; _BB_M = 2.0
_KC_P = 20; _KC_A = 1.5
_SQ_MIN = 3        # 최소 연속 스퀴즈 봉
_WP_MAX = 0.15     # BB20 WP15% VS2.0
_VS_GATE = 2.0
_BB_BTC_FILTER = True


def _tid() -> str:
    return str(uuid.uuid4())[:8]


# ═════════════════════════════════════════════════════════════════
# BB / KC 헬퍼
# ═════════════════════════════════════════════════════════════════
def _calc_bb_15m(closes):
    """BB 상단/중간/하단/width (closes 리스트)."""
    if len(closes) < _BB_P: return None
    w = closes[-_BB_P:]
    ma = sum(w) / _BB_P
    std = (sum((x - ma) ** 2 for x in w) / _BB_P) ** 0.5
    if ma <= 0: return None
    return (ma + _BB_M * std, ma, ma - _BB_M * std, 2 * _BB_M * std / ma)


def _calc_kc_15m(closes, highs, lows):
    """Keltner Channel."""
    if len(closes) < _KC_P + 1: return None
    ma = sum(closes[-_KC_P:]) / _KC_P
    trs = []
    for j in range(-_KC_P, 0):
        if j - 1 < -len(closes): continue
        c_prev = closes[j - 1]
        if c_prev <= 0: continue
        trs.append(max(highs[j] - lows[j], abs(highs[j] - c_prev), abs(lows[j] - c_prev)))
    if not trs: return None
    atr = sum(trs) / len(trs)
    return (ma + _KC_A * atr, ma, ma - _KC_A * atr)


def _bb_width_pctile(closes, lookback=120):
    """현재 BB width의 percentile."""
    if len(closes) < _BB_P + lookback: return 0.5
    widths = []
    for end in range(len(closes) - lookback, len(closes) + 1):
        sl = closes[end - _BB_P:end]
        if len(sl) < _BB_P: continue
        ma = sum(sl) / _BB_P
        std = (sum((x - ma) ** 2 for x in sl) / _BB_P) ** 0.5
        if ma > 0: widths.append(2 * _BB_M * std / ma)
    if len(widths) < 20: return 0.5
    return sum(1 for w in widths if w <= widths[-1]) / len(widths)


def _vol_surge_15m(volumes, fast=5, slow=30):
    if len(volumes) < slow: return 1.0
    vf = sum(volumes[-fast:]) / fast
    vs = sum(volumes[-slow:]) / slow
    return vf / vs if vs > 0 else 1.0


# ═════════════════════════════════════════════════════════════════
# plan_counter
# ═════════════════════════════════════════════════════════════════
def plan_counter(
    snapshot: MarketSnapshot,
    st: Dict,
    system_state: Dict,
) -> List[Intent]:
    """BB Squeeze 브레이크아웃 → MR OR 진입.
    role=CORE_MR → DCA/TP/SL 전부 기존 MR과 동일."""
    from v9.config import (
        COUNTER_ENABLED, COUNTER_COOLDOWN_SEC, COUNTER_MAX,
        COUNTER_SIZE_RATIO,
        DCA_WEIGHTS, LEVERAGE, GRID_DIVISOR,
    )
    if not COUNTER_ENABLED:
        return []

    intents: List[Intent] = []
    now = time.time()
    prices = snapshot.all_prices or {}

    # Cold-start 백필 — 최초 1회
    _backfill_done = getattr(plan_counter, "_backfill_done", False)
    if not _backfill_done:
        plan_counter._backfill_done = True
        _ohlcv_bf = snapshot.ohlcv_pool or {}
        _bf_count = 0
        for _bf_sym, _bf_pool in _ohlcv_bf.items():
            if _bf_sym in _bb_squeeze_count or _bf_sym in _bb_prev_squeeze_len:
                continue
            _bf_1h = _bf_pool.get("1h", [])
            if len(_bf_1h) < _KC_P + 30:
                continue
            _bf_closes = [float(b[4]) for b in _bf_1h]
            _bf_highs = [float(b[2]) for b in _bf_1h]
            _bf_lows = [float(b[3]) for b in _bf_1h]
            _bf_sq = 0
            _bf_psq = 0
            for _bi in range(_KC_P + 1, len(_bf_1h) - max(0, len(_bf_1h) - 60)):
                _end = _bi + 1
                _bf_c = _bf_closes[:_end]
                _bf_h = _bf_highs[:_end]
                _bf_l = _bf_lows[:_end]
                _bbb = _calc_bb_15m(_bf_c)
                _bkc = _calc_kc_15m(_bf_c, _bf_h, _bf_l)
                if not _bbb or not _bkc:
                    continue
                if _bbb[0] < _bkc[0] and _bbb[2] > _bkc[2]:
                    _bf_sq += 1
                else:
                    if _bf_sq > 0:
                        _bf_psq = _bf_sq
                    _bf_sq = 0
            _bb_squeeze_count[_bf_sym] = _bf_sq
            if _bf_psq > 0:
                _bb_prev_squeeze_len[_bf_sym] = _bf_psq
                _bf_count += 1
        if _bf_count > 0:
            print(f"[BB_BACKFILL] {_bf_count}개 심볼 스퀴즈 이력 복구")
            from v9.logging.logger_csv import log_system
            log_system("BB_BACKFILL", f"{_bf_count}개 심볼 스퀴즈 이력 복구")

    # 현재 BB 포지션 수
    cnt_active = 0
    cnt_syms = set()
    for _s, _ss in st.items():
        if not isinstance(_ss, dict): continue
        for _side, _p in iter_positions(_ss):
            if isinstance(_p, dict) and _p.get("entry_type") == "COUNTER":
                cnt_active += 1
                cnt_syms.add(_s)
    if cnt_active >= COUNTER_MAX:
        return []

    # BTC 방향 (12봉 ROC)
    btc_dir = 0
    if _BB_BTC_FILTER:
        _btc_pool = (snapshot.ohlcv_pool or {}).get("BTC/USDT", {})
        _btc_1h = _btc_pool.get("1h", [])
        if len(_btc_1h) >= 13:
            _btc_now = float(_btc_1h[-1][4])
            _btc_prev = float(_btc_1h[-13][4])
            if _btc_prev > 0 and _btc_now > 0:
                _roc = (_btc_now / _btc_prev) - 1
                if _roc > 0.005: btc_dir = 1
                elif _roc < -0.005: btc_dir = -1

    # ── 유니버스 스캔: BB Squeeze 해제 → 브레이크아웃 ──
    _ohlcv_pool = snapshot.ohlcv_pool or {}
    for sym in _ohlcv_pool:
        if cnt_active >= COUNTER_MAX: break

        curr_p = float(prices.get(sym, 0) or 0)
        if curr_p <= 0: continue

        # 이미 포지션 있는 심볼 스킵
        _has_pos = False
        _sym_st = st.get(sym, {})
        if isinstance(_sym_st, dict):
            for _side, _p in iter_positions(_sym_st):
                if isinstance(_p, dict) and float(_p.get("amt", 0) or 0) > 0:
                    _has_pos = True; break
        if _has_pos or sym in cnt_syms: continue

        # 쿨다운
        cd_key = f"{sym}_bb"
        if _bb_cooldowns.get(cd_key, 0) > now: continue

        pool = _ohlcv_pool.get(sym, {})
        ohlcv_1h = pool.get("1h", [])
        if len(ohlcv_1h) < _KC_P + 130: continue

        closes = [float(b[4]) for b in ohlcv_1h]
        highs = [float(b[2]) for b in ohlcv_1h]
        lows = [float(b[3]) for b in ohlcv_1h]
        volumes = [float(b[5]) for b in ohlcv_1h]

        # BB / KC 계산
        bb = _calc_bb_15m(closes)
        kc = _calc_kc_15m(closes, highs, lows)
        if not bb or not kc: continue

        # 스퀴즈 판정
        squeezing = bb[0] < kc[0] and bb[2] > kc[2]

        # 봉 단위 카운팅
        _last_ts = float(ohlcv_1h[-1][0]) if ohlcv_1h else 0
        _prev_ts = _bb_last_bar_ts.get(sym, 0)
        if _last_ts == _prev_ts:
            continue
        _bb_last_bar_ts[sym] = _last_ts

        if squeezing:
            _bb_squeeze_count[sym] = _bb_squeeze_count.get(sym, 0) + 1
        else:
            if _bb_squeeze_count.get(sym, 0) > 0:
                _bb_prev_squeeze_len[sym] = _bb_squeeze_count[sym]
            _bb_squeeze_count[sym] = 0

        if squeezing: continue

        # 최소 스퀴즈 기간 확인
        psq = _bb_prev_squeeze_len.get(sym, 0)
        if psq < _SQ_MIN: continue

        # 직전 봉 BB (돌파 기준)
        bb_prev = _calc_bb_15m(closes[:-1])
        if not bb_prev: continue

        # Width percentile
        wp = _bb_width_pctile(closes[:-1])
        if wp > _WP_MAX:
            _wp_log_key = f"_bb_wp_log_{sym}"
            if now - system_state.get(_wp_log_key, 0) > 300:
                system_state[_wp_log_key] = now
                print(f"[BB_FILTER] {sym} psq={psq} wp={wp:.0%}(>{_WP_MAX:.0%}) — WP 탈락")
                try:
                    from v9.logging.logger_csv import log_system
                    log_system("BB_FILTER", f"{sym} psq={psq} wp={wp:.0%} — WP 탈락")
                except Exception: pass
            continue

        # Volume surge
        vs = _vol_surge_15m(volumes)
        if vs < _VS_GATE:
            print(f"[BB_FILTER] {sym} psq={psq} wp={wp:.0%} vs={vs:.1f}x(<{_VS_GATE}) — VS 탈락")
            try:
                from v9.logging.logger_csv import log_system
                log_system("BB_FILTER", f"{sym} psq={psq} wp={wp:.0%} vs={vs:.1f}x — VS 탈락")
            except Exception: pass
            continue

        # 방향 결정 (BB 돌파)
        entry_side = None
        if curr_p > bb_prev[0]:
            entry_side = "buy"
        elif curr_p < bb_prev[2]:
            entry_side = "sell"
        if not entry_side:
            print(f"[BB_FILTER] {sym} psq={psq} wp={wp:.0%} vs={vs:.1f}x — 돌파 미발생(price={curr_p:.4f} BB=[{bb_prev[2]:.4f},{bb_prev[0]:.4f}])")
            try:
                from v9.logging.logger_csv import log_system
                log_system("BB_FILTER", f"{sym} psq={psq} wp={wp:.0%} vs={vs:.1f}x — 돌파 미발생")
            except Exception: pass
            continue

        # BTC 필터
        if _BB_BTC_FILTER:
            if entry_side == "buy" and btc_dir < 0:
                print(f"[BB_FILTER] {sym} {entry_side} psq={psq} wp={wp:.0%} vs={vs:.1f}x — BTC역방향")
                try:
                    from v9.logging.logger_csv import log_system
                    log_system("BB_FILTER", f"{sym} {entry_side} psq={psq} wp={wp:.0%} vs={vs:.1f}x — BTC역방향")
                except Exception: pass
                continue
            if entry_side == "sell" and btc_dir > 0:
                print(f"[BB_FILTER] {sym} {entry_side} psq={psq} wp={wp:.0%} vs={vs:.1f}x — BTC역방향")
                try:
                    from v9.logging.logger_csv import log_system
                    log_system("BB_FILTER", f"{sym} {entry_side} psq={psq} wp={wp:.0%} vs={vs:.1f}x — BTC역방향")
                except Exception: pass
                continue

        # 한 번만 트리거
        _bb_prev_squeeze_len[sym] = 0

        # ── 진입 ──
        total_cap = float(getattr(snapshot, "real_balance_usdt", 0.0) or 0.0)
        if total_cap <= 0: continue
        grid = (total_cap / GRID_DIVISOR) * LEVERAGE
        tw = sum(DCA_WEIGHTS)
        base_size = grid * DCA_WEIGHTS[0] / tw
        cnt_size = base_size * COUNTER_SIZE_RATIO
        qty = cnt_size / curr_p if curr_p > 0 else 0
        if qty <= 0: continue

        from v9.config import DCA_ENTRY_ROI_BY_TIER
        _dca_targets = []
        for _dt_tier in range(2, len(DCA_WEIGHTS) + 1):
            _dt_roi = DCA_ENTRY_ROI_BY_TIER.get(_dt_tier, -1.8)
            _dt_dist = abs(_dt_roi) / 100 / LEVERAGE
            if entry_side == "buy":
                _dt_p = curr_p * (1.0 - _dt_dist)
            else:
                _dt_p = curr_p * (1.0 + _dt_dist)
            _dt_notional = grid * DCA_WEIGHTS[_dt_tier - 1] / tw
            _dca_targets.append({"tier": _dt_tier, "target_p": _dt_p,
                                  "weight": DCA_WEIGHTS[_dt_tier - 1],
                                  "notional": _dt_notional,
                                  "roi_trigger": _dt_roi})

        intents.append(Intent(
            trace_id=_tid(),
            intent_type=IntentType.OPEN,
            symbol=sym,
            side=entry_side,
            qty=qty,
            price=None,
            reason=f"BB_SQ(sq={psq},wp={wp:.0%},vs={vs:.1f}x)",
            metadata={
                "atr": 0.0,
                "dca_targets": _dca_targets,
                "role": "CORE_MR",
                "entry_type": "COUNTER",
                "positionSide": "LONG" if entry_side == "buy" else "SHORT",
                "locked_regime": "NORMAL",
            },
        ))
        _bb_cooldowns[cd_key] = now + COUNTER_COOLDOWN_SEC
        cnt_active += 1
        cnt_syms.add(sym)

        _dbg = (f"[BB_SQ] ⚡ {sym} {entry_side} "
                f"sq={psq} wp={wp:.0%} vs={vs:.1f}x "
                f"size=${cnt_size:.1f}")
        print(_dbg)
        system_state.setdefault("_counter_tg", []).append(_dbg)

    return intents


# ═════════════════════════════════════════════════════════════════
# 상태 영속화
# ═════════════════════════════════════════════════════════════════
def save_counter_state(system_state: dict):
    system_state["_bb_squeeze_count"] = dict(_bb_squeeze_count)
    system_state["_bb_prev_squeeze_len"] = dict(_bb_prev_squeeze_len)
    system_state["_bb_cooldowns"] = dict(_bb_cooldowns)


def restore_counter_state(system_state: dict):
    global _bb_squeeze_count, _bb_prev_squeeze_len, _bb_cooldowns
    _bb_squeeze_count = system_state.get("_bb_squeeze_count", {})
    _bb_prev_squeeze_len = system_state.get("_bb_prev_squeeze_len", {})
    _bb_cooldowns = system_state.get("_bb_cooldowns", {})
    _bb_active = sum(1 for v in _bb_prev_squeeze_len.values() if v >= _SQ_MIN)
    print(f"[RESTORE] counter: bb_squeeze_ready={_bb_active}syms")
    try:
        from v9.logging.logger_csv import log_system
        log_system("RESTORE", f"counter bb_ready={_bb_active}")
    except Exception:
        pass
