"""
V9 Beta Cycle Engine  (v10.29d — 1h Signal)
====================================================================================================
MR 파이프라인에 통합되는 Beta Cycle 숏 전용 엔진.
Intent 기반: OPEN(숏 진입) / FORCE_CLOSE(청산) 생성 → 기존 risk/execute 경유.

★ V10.29d: 일봉 → 1h 봉 기반으로 전환
  - 1h 봉 자체 버퍼 관리 (250봉 = ~10일)
  - 매 틱에서 새 1h 봉 감지 → excess/ARM/진입 판단
  - return_window=24h, beta_window=168h (7d)
  - 백테스트 1h_r24b168: WR=72%, PF=1.48, MDD=-5.8%

호출 지점 (runner.py):
  1) bc_on_daily_close()  — UTC 00:00 (유니버스 갱신 + 1h 봉 부트스트랩)
  2) bc_on_tick()          — 매 틱 (1h 봉 시그널 + 포지션 관리)

MR 포지션과 분리:
  - role="BC" 태그로 구분
  - MR planners는 _HEDGE_ROLES_SLOT에 "BC" 포함 → 자동 스킵
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
# State (모듈 레벨)
# ═══════════════════════════════════════════════════════════════
_hourly_closes: Dict[str, deque] = {}     # {sym: deque(maxlen=250)} 1h close
_hourly_volumes: Dict[str, deque] = {}    # {sym: deque(maxlen=250)} 1h volume
_btc_hourly: deque = deque(maxlen=250)
_armed: Dict[str, dict] = {}              # {sym: {ts, peak_excess, peak_price, beta, tf, baseline}}
_cooldown_until: Dict[str, float] = {}    # {sym: unix_ts}
_daily_entry_count: int = 0
_last_entry_date: str = ""
_universe: set = set()
_last_hourly_fetch_ts: float = 0          # 마지막 1h fetch 시각
_last_bar_ts: Dict[str, float] = {}       # {sym: last_bar_open_ts} 봉 변경 감지

# ★ V10.29d: 일봉 데이터 (OR 조건용)
_daily_closes: Dict[str, deque] = {}      # {sym: deque(maxlen=90)}
_daily_volumes: Dict[str, deque] = {}
_btc_daily: deque = deque(maxlen=90)
_last_daily_fetch_date: str = ""

_exchange = None


def bc_init(exchange):
    """runner.py 초기화 시 호출."""
    global _exchange
    _exchange = exchange


# ═══════════════════════════════════════════════════════════════
# 일봉 마감 시 호출 (유니버스 갱신 + 1h 부트스트랩)
# ═══════════════════════════════════════════════════════════════
def bc_on_daily_close(snapshot, st: Dict, system_state: Dict) -> List[Intent]:
    """UTC 00:00 — 유니버스 갱신 + 1h 봉 초기 fetch."""
    if not getattr(CFG, 'BC_ENABLED', False):
        return []

    global _daily_entry_count, _last_entry_date
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if today != _last_entry_date:
        _daily_entry_count = 0
        _last_entry_date = today

    # 1h 봉 부트스트랩 (전체 후보풀)
    _fetch_hourly_bars(full=True)
    # ★ V10.29d: 일봉 fetch (OR 조건용)
    _fetch_daily_bars()
    _update_universe()

    return []  # 시그널은 bc_on_tick에서 처리


# ═══════════════════════════════════════════════════════════════
# 매 틱 호출 (1h 시그널 + 포지션 관리)
# ═══════════════════════════════════════════════════════════════
def bc_on_tick(snapshot, st: Dict) -> List[Intent]:
    """매 틱: 1h 봉 업데이트 → 시그널 체크 → 포지션 관리."""
    if not getattr(CFG, 'BC_ENABLED', False):
        return []

    intents: List[Intent] = []

    # ── 1h 봉 업데이트 (ohlcv_pool에서 새 봉 감지) ──
    new_bar_detected = _update_hourly_from_pool(snapshot)

    # ── 매 시간 자체 fetch (유니버스 심볼만 refresh) ──
    if time.time() - _last_hourly_fetch_ts > 3900:  # 65분
        _fetch_hourly_bars(full=False)
        _fetch_daily_bars()  # 하루 1회만 실행됨
        if not _universe:
            _update_universe()
        new_bar_detected = True  # fetch 후 시그널 체크 강제

    # ── 데이터 부족 → 전체 풀 fetch ──
    beta_w = getattr(CFG, 'BC_BETA_WINDOW', 168)
    if len(_btc_hourly) < beta_w + 10:
        # 60초에 1회만 시도
        if time.time() - _last_hourly_fetch_ts > 60:
            _fetch_hourly_bars(full=True)
            _fetch_daily_bars()
            _update_universe()
        return _manage_positions(snapshot, st)

    # ── 새 1h 봉 → 시그널 체크 ──
    if new_bar_detected:
        _check_signals(snapshot, st, intents)

    # ── 포지션 관리 (매 틱) ──
    intents += _manage_positions(snapshot, st)

    return intents


# ═══════════════════════════════════════════════════════════════
# 시그널 판단 (1h 봉 갱신 시 호출)
# ═══════════════════════════════════════════════════════════════
def _check_signals(snapshot, st: Dict, intents: List[Intent]):
    global _daily_entry_count, _last_entry_date

    today = time.strftime("%Y-%m-%d", time.gmtime())
    if today != _last_entry_date:
        _daily_entry_count = 0
        _last_entry_date = today

    # sell side만 체크 (숏 전용)
    held_short_syms = set()
    for sym, sym_st in st.items():
        p = get_p(sym_st, "sell")
        if p and isinstance(p, dict) and float(p.get("amt", 0) or 0) > 0:
            held_short_syms.add(sym)

    bc_count = _count_bc_positions(st)

    for sym in _universe:
        if sym in held_short_syms:
            continue
        if _daily_entry_count >= CFG.BC_ENTRY_PER_DAY:
            break
        if bc_count >= CFG.BC_MAX_POS:
            break
        if sym in _cooldown_until and time.time() < _cooldown_until[sym]:
            continue

        # ★ V10.29d: 1h OR 일봉 excess — 둘 중 하나라도 조건 충족 시 발동
        er_1h = _calc_excess_1h(sym)
        er_d = _calc_excess_daily(sym)

        excess_1h = er_1h[0] if er_1h else None
        beta_1h = er_1h[1] if er_1h else None
        excess_d = er_d[0] if er_d else None
        beta_d = er_d[1] if er_d else None

        if excess_1h is None and excess_d is None:
            continue

        cur_p = _hourly_closes[sym][-1] if sym in _hourly_closes and _hourly_closes[sym] else 0
        if cur_p <= 0:
            continue

        # ── ARMED (OR: 어느 쪽이든 excess ≥ ARM_THRESH) ──
        arm_triggered = False
        arm_excess = 0
        arm_beta = 0
        arm_tf = ""
        if excess_1h is not None and excess_1h >= CFG.BC_ARM_THRESH:
            arm_triggered = True
            arm_excess = excess_1h
            arm_beta = beta_1h or 0
            arm_tf = "1h"
        if excess_d is not None and excess_d >= CFG.BC_ARM_THRESH:
            if not arm_triggered or excess_d > arm_excess:
                arm_triggered = True
                arm_excess = excess_d
                arm_beta = beta_d or 0
                arm_tf = "1d"

        if arm_triggered:
            # ★ V10.29e: 볼륨 확인 — 스파이크에 거래량 동반 필수
            _vol_ok = False
            if sym in _hourly_volumes and len(_hourly_volumes[sym]) >= 48:
                _vols = list(_hourly_volumes[sym])
                _v_recent = sum(_vols[-6:]) / 6 if len(_vols) >= 6 else 0
                _v_avg = sum(_vols[-168:]) / min(168, len(_vols))  # 7일 평균
                _vol_ratio = _v_recent / _v_avg if _v_avg > 0 else 0
                _vol_ok = _vol_ratio >= 1.5  # 최근 6h 볼륨 ≥ 1.5배
            else:
                _vol_ok = True  # 데이터 부족 시 패스

            if not _vol_ok:
                continue  # 볼륨 없는 스파이크 → 무시

            if sym not in _armed:
                _bl = _calc_baseline_excess(sym)
                _armed[sym] = {
                    "ts": time.time(),
                    "peak_excess": arm_excess,
                    "peak_price": cur_p,
                    "beta": arm_beta,
                    "tf": arm_tf,
                    "baseline": _bl if _bl is not None else 0.0,
                }
                print(f"[BC] 🔔 ARMED {sym} excess={arm_excess:+.1%} β↑={arm_beta:.2f} tf={arm_tf} baseline={_armed[sym]['baseline']:+.1%}")
            else:
                if arm_excess > _armed[sym]["peak_excess"]:
                    _armed[sym]["peak_excess"] = arm_excess
                if cur_p > _armed[sym]["peak_price"]:
                    _armed[sym]["peak_price"] = cur_p
                # ★ V10.29e: 1d가 나중에 트리거되면 tf 승격 + 만료 연장
                if arm_tf == "1d" and _armed[sym].get("tf") == "1h":
                    _armed[sym]["tf"] = "1d"
                    _armed[sym]["ts"] = time.time()

        # ── SHORT 진입: 1h excess가 baseline(72h 정상 수준)으로 복귀해야 진입 ──
        # ★ V10.30: 고정 NORM_THRESH 제거 → 심볼별 baseline 적응형
        norm_triggered = False
        if sym in _armed:
            _bl = _armed[sym].get("baseline", 0.0)
            # 1h excess가 baseline 이하 = "진짜 식었다"
            if excess_1h is not None and excess_1h <= _bl:
                norm_triggered = True

        # ★ V10.29e: excess 완전 되돌림(≤0) → ARMED 해제 (기회 소멸)
        if sym in _armed:
            _all_exhausted = True
            if excess_1h is not None and excess_1h > 0:
                _all_exhausted = False
            if excess_d is not None and excess_d > 0:
                _all_exhausted = False
            if _all_exhausted and (excess_1h is not None or excess_d is not None):
                print(f"[BC] ❌ DISARM {sym} excess 완전 되돌림 (1h={excess_1h}, 1d={excess_d})")
                _armed.pop(sym, None)
                continue

        if sym in _armed and norm_triggered:
            arm = _armed[sym]

            pullback = (arm["peak_price"] - cur_p) / arm["peak_price"] if arm["peak_price"] > 0 else 0
            if pullback > CFG.BC_PULLBACK_MAX:
                print(f"[BC] ⏭ SKIP {sym} pullback={pullback:.1%} > max")
                _armed.pop(sym, None)
                continue
            if pullback < CFG.BC_PULLBACK_MIN:
                continue

            equity = float(getattr(snapshot, 'real_balance_usdt', 0) or 0)
            if equity <= 0:
                continue
            notional = equity / CFG.BC_SIZE_DIVISOR
            price = float((snapshot.all_prices or {}).get(sym, cur_p))
            if price <= 0:
                continue
            qty = notional / price

            min_qty = CFG.SYM_MIN_QTY.get(sym, CFG.SYM_MIN_QTY_DEFAULT)
            if qty < min_qty or notional < 10:
                continue

            # 현재 excess (로그용)
            _ex_1h_str = f"1h={excess_1h:+.1%}" if excess_1h is not None else "1h=N/A"
            _ex_d_str = f"1d={excess_d:+.1%}" if excess_d is not None else "1d=N/A"

            ensure_slot(st, sym)

            intent = Intent(
                trace_id=f"BC_{uuid.uuid4().hex[:8]}",
                intent_type=IntentType.OPEN,
                symbol=sym,
                side="sell",
                qty=qty,
                price=None,
                reason=f"BC_SHORT peak={arm['peak_excess']:+.1%} {_ex_1h_str} {_ex_d_str} pb={pullback:.1%} bl={arm.get('baseline',0):+.1%}",
                metadata={
                    "positionSide": "SHORT",
                    "role": "BC",
                    "bc_peak_excess": arm["peak_excess"],
                    "bc_beta": arm.get("beta", 0),
                    "bc_baseline": arm.get("baseline", 0.0),
                    "bc_entry_ts": time.time(),
                },
            )
            intents.append(intent)
            _armed.pop(sym, None)
            _daily_entry_count += 1
            bc_count += 1
            held_short_syms.add(sym)

            print(f"[BC] 📉 SHORT {sym} peak={arm['peak_excess']:+.1%} {_ex_1h_str} {_ex_d_str} "
                  f"pb={pullback:.1%} β↑={arm.get('beta',0):.2f} "
                  f"qty={qty:.4f} ${notional:.0f} [{_daily_entry_count}/{CFG.BC_ENTRY_PER_DAY}]")

        # ARMED 만료 — ★ V10.29e: 1h/1d 별도 만료
        if sym in _armed:
            age_h = (time.time() - _armed[sym]["ts"]) / 3600
            _tf = _armed[sym].get("tf", "1h")
            expiry_h = 48 if _tf == "1h" else 168  # 1h→2일, 1d→7일
            if age_h > expiry_h:
                _armed.pop(sym, None)


# ═══════════════════════════════════════════════════════════════
# 포지션 관리 (SL/TP/Trail/Timeout)
# ═══════════════════════════════════════════════════════════════
def _manage_positions(snapshot, st: Dict) -> List[Intent]:
    intents: List[Intent] = []

    for sym, sym_st in st.items():
        p = get_p(sym_st, "sell")
        if not p or not isinstance(p, dict):
            continue
        if p.get("role") != "BC":
            continue

        price = float((snapshot.all_prices or {}).get(sym, 0))
        if price <= 0:
            continue

        ep = float(p.get("ep", 0) or 0)
        if ep <= 0:
            continue

        entry_ts = float(p.get("time", 0) or 0) or time.time()
        hold_hours = (time.time() - entry_ts) / 3600
        roi = (ep - price) / ep  # 숏: 양수=수익

        # 1h high (ohlcv_pool에서)
        ohlcv = (snapshot.ohlcv_pool or {}).get(sym, {}).get('1h', [])
        h_1h = float(ohlcv[-2][2]) if ohlcv and len(ohlcv) >= 2 else price

        # ATR 기반 트레일
        atr_pct = _calc_atr_1h(ohlcv)
        trail_offset = max(getattr(CFG, 'BC_TRAIL_FLOOR', 0.015),
                          atr_pct * getattr(CFG, 'BC_TRAIL_ATR_MULT', 1.5))

        # 트레일 저점 갱신
        trail_low = float(p.get("bc_trail_low", ep) or ep)
        if price < trail_low:
            trail_low = price
            p["bc_trail_low"] = trail_low

        # 트레일 활성화
        trail_active = p.get("bc_trail_active", False)
        if not trail_active and roi >= getattr(CFG, 'BC_TRAIL_ACTIVATION', 0.03):
            p["bc_trail_active"] = True
            trail_active = True

        # ── 청산 판단 ──
        reason = None

        sl_price = ep * (1 + CFG.BC_SHORT_SL / 100)
        if price >= sl_price or h_1h >= sl_price:
            reason = "BC_SL"
        elif price <= ep * (1 - CFG.BC_SHORT_TP / 100):
            reason = "BC_TP"
        elif trail_active:
            trail_stop = trail_low * (1 + trail_offset)
            if price >= trail_stop or h_1h >= trail_stop:
                reason = "BC_TRAIL"
        elif hold_hours >= CFG.BC_MAX_HOLD_HOURS:
            reason = "BC_TIMEOUT"

        # ★ V10.30: excess 재상승 → thesis 실패 손절
        # baseline 복귀 후 진입했으므로 ARM_THRESH까지 충분한 갭 확보됨
        if not reason:
            _er = _calc_excess_1h(sym)
            if _er and _er[0] >= CFG.BC_ARM_THRESH:
                reason = f"BC_REHEAT(ex={_er[0]:+.1%})"

        if reason:
            amt = float(p.get("amt", 0) or 0)
            if amt <= 0:
                continue
            intents.append(Intent(
                trace_id=f"BC_{uuid.uuid4().hex[:8]}",
                intent_type=IntentType.FORCE_CLOSE,
                symbol=sym,
                side="buy",
                qty=amt,
                price=None,
                reason=f"{reason} roi={roi:+.1%} hold={hold_hours:.0f}h",
                metadata={
                    "positionSide": "SHORT",
                    "role": "BC",
                    "_expected_role": "BC",
                },
            ))
            cd_hours = getattr(CFG, 'BC_COOLDOWN_HOURS', 72)
            _cooldown_until[sym] = time.time() + cd_hours * 3600
            print(f"[BC] {'✅' if roi > 0 else '❌'} {reason} {sym} "
                  f"roi={roi:+.1%} hold={hold_hours:.0f}h")

    return intents


# ═══════════════════════════════════════════════════════════════
# 1h 봉 관리
# ═══════════════════════════════════════════════════════════════

def _update_hourly_from_pool(snapshot) -> bool:
    """ohlcv_pool에서 새 1h 봉 감지 → 버퍼 업데이트. 새 봉 있으면 True."""
    pool = snapshot.ohlcv_pool if snapshot else {}
    if not pool:
        return False

    new_bar = False
    buf_size = getattr(CFG, 'BC_1H_BUFFER_SIZE', 250)

    # BTC
    btc_1h = pool.get("BTC/USDT", {}).get("1h", [])
    if btc_1h and len(btc_1h) >= 2:
        last_bar = btc_1h[-2]  # 마감된 직전 봉
        bar_ts = float(last_bar[0])
        if bar_ts != _last_bar_ts.get("BTC/USDT", 0):
            _last_bar_ts["BTC/USDT"] = bar_ts
            _btc_hourly.append(float(last_bar[4]))
            new_bar = True

    # 유니버스 심볼 + armed 심볼
    check_syms = _universe | set(_armed.keys())
    for sym in check_syms:
        sym_1h = pool.get(sym, {}).get("1h", [])
        if not sym_1h or len(sym_1h) < 2:
            continue
        last_bar = sym_1h[-2]
        bar_ts = float(last_bar[0])
        if bar_ts != _last_bar_ts.get(sym, 0):
            _last_bar_ts[sym] = bar_ts
            if sym not in _hourly_closes:
                _hourly_closes[sym] = deque(maxlen=buf_size)
                _hourly_volumes[sym] = deque(maxlen=buf_size)
            _hourly_closes[sym].append(float(last_bar[4]))
            _hourly_volumes[sym].append(float(last_bar[5]))

    return new_bar


def _fetch_daily_bars():
    """★ V10.29d: 1일 1회 일봉 데이터 갱신 (OR 조건용)."""
    global _last_daily_fetch_date
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if today == _last_daily_fetch_date:
        return
    if _exchange is None:
        return

    _last_daily_fetch_date = today
    print(f"[BC] 📥 Fetching daily bars...")

    try:
        btc_bars = _exchange.fetch_ohlcv("BTC/USDT", "1d", limit=90)
        _btc_daily.clear()
        for b in btc_bars:
            _btc_daily.append(float(b[4]))
    except Exception as e:
        print(f"[BC] BTC 1d fetch 실패: {e}")
        return

    pool = getattr(CFG, 'BC_CANDIDATE_POOL', _DEFAULT_POOL)
    for sym in pool:
        try:
            bars = _exchange.fetch_ohlcv(sym, "1d", limit=90)
            _daily_closes[sym] = deque(maxlen=90)
            _daily_volumes[sym] = deque(maxlen=90)
            for b in bars:
                _daily_closes[sym].append(float(b[4]))
                _daily_volumes[sym].append(float(b[5]))
        except Exception:
            pass
        time.sleep(0.05)

    print(f"[BC] ✅ Daily bars: BTC({len(_btc_daily)}) + {len(_daily_closes)} alts")


def _fetch_hourly_bars(full: bool = False):
    """1h 봉 fetch. full=True: 전체 후보풀 (부트스트랩), False: 유니버스만 (매시간)."""
    global _last_hourly_fetch_ts
    if _exchange is None:
        return

    now = time.time()
    _last_hourly_fetch_ts = now
    buf_size = getattr(CFG, 'BC_1H_BUFFER_SIZE', 250)

    # full이면 전체 후보풀, 아니면 유니버스 + armed만
    if full or not _universe:
        syms = getattr(CFG, 'BC_CANDIDATE_POOL', _DEFAULT_POOL)
        mode = "bootstrap"
    else:
        syms = list(_universe | set(_armed.keys()))
        mode = "refresh"

    print(f"[BC] 📥 Fetching 1h bars ({mode}, {len(syms)+1}개, buf={buf_size})...")

    # BTC
    try:
        bars = _exchange.fetch_ohlcv("BTC/USDT", "1h", limit=buf_size)
        _btc_hourly.clear()
        for b in bars:
            _btc_hourly.append(float(b[4]))
    except Exception as e:
        print(f"[BC] BTC 1h fetch 실패: {e}")
        return

    for sym in syms:
        try:
            bars = _exchange.fetch_ohlcv(sym, "1h", limit=buf_size)
            _hourly_closes[sym] = deque(maxlen=buf_size)
            _hourly_volumes[sym] = deque(maxlen=buf_size)
            for b in bars:
                _hourly_closes[sym].append(float(b[4]))
                _hourly_volumes[sym].append(float(b[5]))
        except Exception as e:
            print(f"[BC] {sym} 1h fetch 실패(무시): {e}")
        time.sleep(0.05)

    print(f"[BC] ✅ 1h bars: BTC({len(_btc_hourly)}) + {len(_hourly_closes)} alts")


def _update_universe():
    """★ V10.29d: ARM→NORM 사이클 기반 유니버스 스코어링.

    score = arm_hit_rate × 0.4 + norm_success_rate × 0.35 + mr_tendency × 0.25
      - arm_hit_rate:      lookback 구간에서 excess ≥ ARM 비율 (빈도)
      - norm_success_rate: ARM 이벤트 중 excess가 0~NORM으로 복귀한 비율 (품질)
      - mr_tendency:       excess 자기상관 반전 강도 (되돌림 경향)
    """
    global _universe
    beta_w = getattr(CFG, 'BC_BETA_WINDOW', 168)
    ret_w = getattr(CFG, 'BC_RETURN_WINDOW', 24)
    arm_th = getattr(CFG, 'BC_ARM_THRESH', 0.05)
    # ★ V10.30: 유니버스 점수용 정규화 임계치 (baseline 근사값)
    # 실제 진입은 심볼별 baseline 사용, 여기는 순위 산정용 고정값
    norm_th = 0.02

    if len(_btc_hourly) < beta_w + 65:
        return

    scores = []
    btc_c = list(_btc_hourly)

    for sym, dc in _hourly_closes.items():
        c = list(dc)
        if len(c) < beta_w + 65:
            continue

        # excess 시계열 구축
        excess_hist = []
        for di in range(max(0, len(c) - 120), len(c)):
            if di < ret_w + beta_w + 1 or di >= len(btc_c):
                continue
            try:
                alt_ret = (c[di] / c[di - ret_w]) - 1
                btc_ret = (btc_c[di] / btc_c[di - ret_w]) - 1
                alt_lr = np.diff(np.log(c[di - beta_w:di + 1]))
                btc_lr = np.diff(np.log(btc_c[di - beta_w:di + 1]))
                n = min(len(alt_lr), len(btc_lr))
                if n < 20:
                    continue
                var_b = np.var(btc_lr[-n:])
                if var_b < 1e-15:
                    continue
                beta = float(np.cov(alt_lr[-n:], btc_lr[-n:])[0][1] / var_b)
                excess = alt_ret - (beta * btc_ret)
                excess_hist.append(excess)
            except Exception:
                pass

        if len(excess_hist) < 30:
            continue

        ea = np.array(excess_hist)
        total_bars = len(ea)

        # ── (1) arm_hit_rate: ARM 달성 빈도 ──
        arm_hits = int(np.sum(ea >= arm_th))
        arm_hit_rate = arm_hits / total_bars if total_bars > 0 else 0

        # ── (2) norm_success_rate: ARM→NORM 사이클 완료 비율 ──
        arm_events = 0
        norm_events = 0
        in_armed = False
        for ex_val in ea:
            if not in_armed and ex_val >= arm_th:
                in_armed = True
                arm_events += 1
            elif in_armed and 0 <= ex_val <= norm_th:
                norm_events += 1
                in_armed = False
            elif in_armed and ex_val < 0:
                # excess 음수 = 과도하게 떨어짐 (진입 스킵 대상)
                in_armed = False
        norm_success = norm_events / max(1, arm_events)

        # ── (3) mr_tendency: 자기상관 반전 ──
        lag = min(5, len(ea) // 3)
        try:
            mr = -float(np.corrcoef(ea[:-lag], ea[lag:])[0][1]) if len(ea) > lag * 2 else 0.0
        except Exception:
            mr = 0.0
        if np.isnan(mr):
            mr = 0.0

        # ── 최종 스코어 ──
        # arm_events가 0이면 진입 불가 → 스킵
        if arm_events == 0:
            continue

        score = arm_hit_rate * 0.4 + norm_success * 0.35 + max(0, mr) * 0.25
        scores.append((sym, score, arm_events, norm_events))

    scores.sort(key=lambda x: -x[1])
    top_n = getattr(CFG, 'BC_UNI_TOP_N', 20)
    _universe = {s[0] for s in scores[:top_n]}
    if _universe:
        top3 = scores[:3]
        detail = ", ".join(f"{s[0].replace('/USDT','')}({s[2]}→{s[3]},{s[1]:.2f})"
                          for s in top3)
        print(f"[BC] 🌐 Universe: {len(_universe)}개 top3=[{detail}]")


# ═══════════════════════════════════════════════════════════════
# 내부 계산
# ═══════════════════════════════════════════════════════════════

def _count_bc_positions(st: Dict) -> int:
    count = 0
    for sym, sym_st in st.items():
        p = get_p(sym_st, "sell")
        if p and isinstance(p, dict) and p.get("role") == "BC":
            count += 1
    return count


def _calc_excess_1h(sym) -> Optional[Tuple[float, float]]:
    """1h 버퍼에서 excess return + beta 계산."""
    beta_w = getattr(CFG, 'BC_BETA_WINDOW', 168)
    ret_w = getattr(CFG, 'BC_RETURN_WINDOW', 24)

    if sym not in _hourly_closes or len(_hourly_closes[sym]) < beta_w + 5:
        return None
    if len(_btc_hourly) < beta_w + 5:
        return None

    ac = list(_hourly_closes[sym])
    bc = list(_btc_hourly)

    if len(ac) < ret_w + 1 or len(bc) < ret_w + 1:
        return None
    if ac[-1] <= 0 or ac[-(ret_w + 1)] <= 0 or bc[-1] <= 0 or bc[-(ret_w + 1)] <= 0:
        return None

    alt_ret = (ac[-1] / ac[-(ret_w + 1)]) - 1
    btc_ret = (bc[-1] / bc[-(ret_w + 1)]) - 1

    try:
        alt_lr = np.diff(np.log(ac[-(beta_w + 1):]))
        btc_lr = np.diff(np.log(bc[-(beta_w + 1):]))
        n = min(len(alt_lr), len(btc_lr))
        if n < 20:
            return None
        # ★ V10.29e: 상방 베타 — BTC 상승 봉만 사용 (하락 반등 숏 방지)
        _up_mask = btc_lr[-n:] > 0
        if np.sum(_up_mask) < 10:
            return None
        _alt_up = alt_lr[-n:][_up_mask]
        _btc_up = btc_lr[-n:][_up_mask]
        var_b = np.var(_btc_up)
        if var_b < 1e-15:
            return None
        beta = float(np.cov(_alt_up, _btc_up)[0][1] / var_b)
    except Exception:
        return None

    excess = alt_ret - (beta * btc_ret)
    return (excess, beta)


def _calc_excess_daily(sym) -> Optional[Tuple[float, float]]:
    """★ V10.29d: 일봉 버퍼에서 excess return + beta 계산 (OR 조건용)."""
    RET_W = 7   # 7일 return
    BETA_W = 30  # 30일 beta

    if sym not in _daily_closes or len(_daily_closes[sym]) < BETA_W + 5:
        return None
    if len(_btc_daily) < BETA_W + 5:
        return None

    ac = list(_daily_closes[sym])
    bc = list(_btc_daily)

    if len(ac) < RET_W + 1 or len(bc) < RET_W + 1:
        return None
    if ac[-1] <= 0 or ac[-(RET_W + 1)] <= 0 or bc[-1] <= 0 or bc[-(RET_W + 1)] <= 0:
        return None

    alt_ret = (ac[-1] / ac[-(RET_W + 1)]) - 1
    btc_ret = (bc[-1] / bc[-(RET_W + 1)]) - 1

    try:
        alt_lr = np.diff(np.log(ac[-(BETA_W + 1):]))
        btc_lr = np.diff(np.log(bc[-(BETA_W + 1):]))
        n = min(len(alt_lr), len(btc_lr))
        if n < 10:
            return None
        # ★ V10.29e: 상방 베타 — BTC 상승 봉만 사용
        _up_mask = btc_lr[-n:] > 0
        if np.sum(_up_mask) < 5:
            return None
        _alt_up = alt_lr[-n:][_up_mask]
        _btc_up = btc_lr[-n:][_up_mask]
        var_b = np.var(_btc_up)
        if var_b < 1e-15:
            return None
        beta = float(np.cov(_alt_up, _btc_up)[0][1] / var_b)
    except Exception:
        return None

    excess = alt_ret - (beta * btc_ret)
    return (excess, beta)


def _calc_baseline_excess(sym) -> Optional[float]:
    """★ V10.30: ARM 직전 72h 중앙값 excess (스파이크 전 "정상" 수준).

    최근 SKIP(24h)을 제외하고 그 이전 WINDOW(72h) 구간에서
    매 시점의 24h excess return을 계산 → 중앙값 반환.
    """
    beta_w = getattr(CFG, 'BC_BETA_WINDOW', 168)
    ret_w = getattr(CFG, 'BC_RETURN_WINDOW', 24)
    skip = getattr(CFG, 'BC_BASELINE_SKIP', 24)
    window = getattr(CFG, 'BC_BASELINE_WINDOW', 72)

    ac = list(_hourly_closes.get(sym, []))
    bc = list(_btc_hourly)
    need = max(beta_w, skip + window + ret_w) + 5
    if len(ac) < need or len(bc) < need:
        return None

    # beta — _calc_excess_1h와 동일 방식
    try:
        alt_lr = np.diff(np.log(ac[-(beta_w + 1):]))
        btc_lr = np.diff(np.log(bc[-(beta_w + 1):]))
        n = min(len(alt_lr), len(btc_lr))
        if n < 20:
            return None
        _up_mask = btc_lr[-n:] > 0
        if np.sum(_up_mask) < 10:
            return None
        _alt_up = alt_lr[-n:][_up_mask]
        _btc_up = btc_lr[-n:][_up_mask]
        var_b = np.var(_btc_up)
        if var_b < 1e-15:
            return None
        beta = float(np.cov(_alt_up, _btc_up)[0][1] / var_b)
    except Exception:
        return None

    excesses = []
    for t in range(skip, skip + window):
        idx_now = -(t + 1)
        idx_ago = -(t + 1 + ret_w)
        if abs(idx_ago) >= len(ac) or abs(idx_ago) >= len(bc):
            continue
        a_now, a_ago = ac[idx_now], ac[idx_ago]
        b_now, b_ago = bc[idx_now], bc[idx_ago]
        if a_now <= 0 or a_ago <= 0 or b_now <= 0 or b_ago <= 0:
            continue
        alt_ret = (a_now / a_ago) - 1
        btc_ret = (b_now / b_ago) - 1
        excesses.append(alt_ret - beta * btc_ret)

    if len(excesses) < 10:
        return None
    return float(np.median(excesses))


def _calc_atr_1h(ohlcv_1h) -> float:
    """1h ohlcv에서 ATR % 계산."""
    if not ohlcv_1h or len(ohlcv_1h) < 26:
        return 0.02

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


# 기본 후보 풀
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
# State 영속화
# ═══════════════════════════════════════════════════════════════
def bc_save_state(system_state: dict):
    system_state["_bc_armed"] = dict(_armed)
    system_state["_bc_cooldown_until"] = dict(_cooldown_until)
    system_state["_bc_daily_entry_count"] = _daily_entry_count
    system_state["_bc_last_entry_date"] = _last_entry_date


def bc_restore_state(system_state: dict):
    global _armed, _cooldown_until, _daily_entry_count, _last_entry_date
    _armed = system_state.get("_bc_armed", {})
    _cooldown_until = system_state.get("_bc_cooldown_until", {})
    _daily_entry_count = system_state.get("_bc_daily_entry_count", 0)
    _last_entry_date = system_state.get("_bc_last_entry_date", "")
    if _armed:
        print(f"[BC_RESTORE] armed={list(_armed.keys())} cd={len(_cooldown_until)}")
    else:
        print(f"[BC_RESTORE] armed=0 cd={len(_cooldown_until)}")
    try:
        from v9.logging.logger_csv import log_system
        log_system("BC_RESTORE", f"armed={len(_armed)} cd={len(_cooldown_until)}")
    except Exception:
        pass
