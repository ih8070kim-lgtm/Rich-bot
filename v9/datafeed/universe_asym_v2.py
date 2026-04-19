"""
V9 Datafeed — Universe ASYM v2  (v10.15)
==========================================
★ v10.15: 롱/숏 완전 분리 파이프라인
  공통: 메이저 유니버스 → 최소 거래량 floor → 펀딩비 분리 → 바이어스
  이후: Long 파이프라인 / Short 파이프라인 독립 실행
    각 파이프라인: 거래량 상대컷 → corr → beta → Hurst → ATR 랭킹 → N개 선발
"""
import asyncio
import time
import uuid

import numpy as np

from v9.config import (
    GLOBAL_BLACKLIST,
    MAJOR_UNIVERSE,
    UNIVERSE_EXCLUDE_TOP_ATR,
    UNIVERSE_LONG_N,
    UNIVERSE_MAX_CORR,
    UNIVERSE_CORR_WHITELIST,
    UNIVERSE_SHORT_N,
    UNIVERSE_STICKY_MIN_SEC,
    UNIVERSE_TOP_N,
    UNIVERSE_VOL_FLOOR_USD,
    UNIVERSE_MIN_POOL_SIZE,
    LONG_MIN_CORR, LONG_BETA_MIN, LONG_BETA_MAX,
    SHORT_MIN_CORR, SHORT_BETA_MIN, SHORT_BETA_MAX,
)
from v9.logging.logger_csv import log_universe
from v9.types import MarketSnapshot
from v9.utils.utils_math import atr_from_ohlcv, log_returns, safe_corr

_sticky_long: dict = {}
_sticky_short: dict = {}

HURST_BOOST_THRESH = 0.70
HURST_PENALTY_THRESH = 0.85

def _calc_hurst(closes: np.ndarray, max_lag: int = 20) -> float:
    if len(closes) < max_lag * 2:
        return 0.5
    log_ret = np.diff(np.log(closes))
    lags = range(2, max_lag + 1)
    rs_list = []
    for lag in lags:
        chunks = [log_ret[i:i+lag] for i in range(0, len(log_ret) - lag + 1, lag)]
        if len(chunks) < 2:
            continue
        rs_vals = []
        for chunk in chunks:
            m = np.mean(chunk)
            deviate = np.cumsum(chunk - m)
            r = np.max(deviate) - np.min(deviate)
            s = np.std(chunk, ddof=1)
            if s > 0:
                rs_vals.append(r / s)
        if rs_vals:
            rs_list.append((lag, np.mean(rs_vals)))
    if len(rs_list) < 3:
        return 0.5
    log_lags = np.log([x[0] for x in rs_list])
    log_rs = np.log([x[1] for x in rs_list])
    H = np.polyfit(log_lags, log_rs, 1)[0]
    return float(np.clip(H, 0.0, 1.0))


def _fetch_funding_map(raw_fr: dict) -> dict:
    result = {}
    for k, v in raw_fr.items():
        sym_key = k
        if ":" in sym_key:
            sym_key = sym_key.split(":")[0]
        sym_key = sym_key.replace("_", "/")
        if not sym_key.endswith("/USDT"):
            continue
        fr_val = None
        if isinstance(v, dict):
            fr_val = v.get("fundingRate") or v.get("funding_rate")
        elif isinstance(v, (int, float)):
            fr_val = v
        if fr_val is not None:
            try:
                result[sym_key] = float(fr_val)
            except (TypeError, ValueError):
                pass
    return result


async def update_universe(ex, snapshot: MarketSnapshot) -> MarketSnapshot:
    """
    ★ v10.15 Universe — 롱/숏 분리 파이프라인
    """
    global _sticky_long, _sticky_short

    try:
        tickers = await asyncio.to_thread(ex.fetch_tickers)

        # ══ Step 1: 공통 — 절대 floor + 거래량 수집 ══
        all_vols = {}
        for sym in MAJOR_UNIVERSE:
            if sym in GLOBAL_BLACKLIST:
                continue
            ticker_key = f"{sym}:USDT" if f"{sym}:USDT" in tickers else sym
            if ticker_key not in tickers:
                continue
            vol = float(tickers[ticker_key].get("quoteVolume", 0) or 0)
            if vol >= UNIVERSE_VOL_FLOOR_USD:
                all_vols[sym] = vol

        if not all_vols:
            print("[Universe V10.15] vol_list empty — skip")
            return snapshot

        # ══ Step 2: 공통 — 펀딩비 분리 + 바이어스 ══
        funding_map: dict = {}
        try:
            raw_fr = await asyncio.to_thread(ex.fetch_funding_rates)
            funding_map = _fetch_funding_map(raw_fr)
        except Exception as fe:
            print(f"[Universe V10.15] 펀딩비 fetch 실패 (전체 풀 폴백): {fe}")

        vol_syms = sorted(all_vols.keys(), key=lambda s: all_vols[s], reverse=True)

        if funding_map:
            fr_sorted = sorted(vol_syms, key=lambda s: funding_map.get(s, 0.0))
            mid = len(fr_sorted) // 2
            long_pool = set(fr_sorted[:mid])
            short_pool = set(fr_sorted[mid:])
        else:
            long_pool = set(vol_syms)
            short_pool = set(vol_syms)

        from v9.config import LONG_ONLY_SYMBOLS, SHORT_ONLY_SYMBOLS
        long_pool = (long_pool - SHORT_ONLY_SYMBOLS) | (long_pool & LONG_ONLY_SYMBOLS)
        short_pool = (short_pool - LONG_ONLY_SYMBOLS) | (short_pool & SHORT_ONLY_SYMBOLS)
        for sym in LONG_ONLY_SYMBOLS:
            if sym in all_vols and sym not in long_pool:
                long_pool.add(sym)
                short_pool.discard(sym)
        for sym in SHORT_ONLY_SYMBOLS:
            if sym in all_vols and sym not in short_pool:
                short_pool.add(sym)
                long_pool.discard(sym)

        print(f"[Universe V10.15] 풀 분리: L={len(long_pool)} S={len(short_pool)} "
              f"(floor={len(all_vols)}종목)")

        # ══ Step 3: BTC OHLCV (공통) ══
        try:
            btc_ohlcv = await asyncio.to_thread(ex.fetch_ohlcv, "BTC/USDT", "1h", limit=50)
            if len(btc_ohlcv) < 24:
                return snapshot
            btc_closes = np.array([float(x[4]) for x in btc_ohlcv])
            btc_lr = log_returns(btc_closes)
        except Exception as e:
            print(f"[Universe V10.15] BTC OHLCV error: {e}")
            return snapshot

        _btc_std = float(np.std(btc_lr)) if len(btc_lr) > 1 else 1.0
        new_correlations = dict(snapshot.correlations)
        _excluded_log = []

        # ══ Step 4: 분리 파이프라인 함수 ══
        async def _pipeline(pool_syms, pool_name, min_corr, beta_min, beta_max, target_n):
            # 거래량 상대 컷 (하위 25%)
            pool_vols = sorted(
                [(s, all_vols[s]) for s in pool_syms if s in all_vols],
                key=lambda x: x[1], reverse=True
            )
            cut_idx = max(1, len(pool_vols) * 3 // 4)
            active_syms = {s for s, _ in pool_vols[:cut_idx]}
            cut_syms = [s for s, _ in pool_vols[cut_idx:]]
            if cut_syms:
                print(f"[Universe V10.15] {pool_name} 거래량 하위25% 컷: {cut_syms}")

            cands = []
            for sym_name, _ in pool_vols:
                if sym_name not in active_syms:
                    continue
                try:
                    alt_ohlcv = await asyncio.to_thread(ex.fetch_ohlcv, sym_name, "1h", limit=50)
                    if len(alt_ohlcv) < 24:
                        continue

                    alt_closes = np.array([float(x[4]) for x in alt_ohlcv])
                    alt_lr = log_returns(alt_closes)
                    corr_24h = safe_corr(btc_lr, alt_lr)
                    new_correlations[sym_name] = corr_24h

                    if corr_24h < min_corr:
                        _excluded_log.append(f"{sym_name}(corr={corr_24h:.2f}<{min_corr},{pool_name})")
                        continue
                    if corr_24h > UNIVERSE_MAX_CORR and sym_name not in UNIVERSE_CORR_WHITELIST:
                        continue

                    _alt_std = float(np.std(alt_lr)) if len(alt_lr) > 1 else 0.0
                    _beta = corr_24h * (_alt_std / _btc_std) if _btc_std > 0 else 1.0
                    if _beta < beta_min or _beta > beta_max:
                        _excluded_log.append(f"{sym_name}(b={_beta:.2f},{pool_name})")
                        continue

                    atr_val = atr_from_ohlcv(alt_ohlcv, period=10)
                    curr_price = float(alt_ohlcv[-1][4])
                    if curr_price <= 0:
                        continue
                    atr_pct = atr_val / curr_price if atr_val > 0 else 0.0

                    _hurst = _calc_hurst(alt_closes)
                    if _hurst < HURST_BOOST_THRESH:
                        _h_mult = 1.2
                    elif _hurst >= HURST_PENALTY_THRESH:
                        _h_mult = 0.7
                    else:
                        _h_mult = 1.0

                    cands.append({
                        "sym": sym_name,
                        "corr_24h": corr_24h,
                        "atr_pct": atr_pct * _h_mult,
                        "beta": _beta,
                        "hurst": _hurst,
                    })
                    await asyncio.sleep(0.05)
                except Exception:
                    continue

            # ★ V10.31e: PnL score tiebreaker — ATR 랭킹에 최근 실적 가중치
            # 과거 실적은 후행지표. 과적합 리스크 인지한 상태에서 SYMBOL_PNL_WEIGHT 0.2로 소폭 적용.
            # PnL score 범위 -1.0 ~ +1.0. combined_score = atr_pct * (1 + weight * pnl_score)
            # weight=0 이면 기존 ATR 랭킹 동일 (즉시 원복 가능)
            try:
                from v9.config import SYMBOL_STATS_ENABLED, SYMBOL_PNL_WEIGHT
                if SYMBOL_STATS_ENABLED and SYMBOL_PNL_WEIGHT > 0:
                    from v9.strategy.symbol_stats import get_pnl_score
                    for c in cands:
                        _psc = get_pnl_score(c["sym"])
                        c["pnl_score"] = _psc
                        c["combined"] = c["atr_pct"] * (1.0 + SYMBOL_PNL_WEIGHT * _psc)
                    cands.sort(key=lambda x: x.get("combined", x["atr_pct"]), reverse=True)
                else:
                    cands.sort(key=lambda x: x["atr_pct"], reverse=True)
            except Exception:
                cands.sort(key=lambda x: x["atr_pct"], reverse=True)

            cands = cands[UNIVERSE_EXCLUDE_TOP_ATR:]
            cands = cands[:UNIVERSE_TOP_N]
            selected = [x["sym"] for x in cands[:target_n]]

            # ★ V10.30: 선발 심볼 beta/corr 로그
            # ★ V10.31e: PnL score도 표시 (SYMBOL_PNL_WEIGHT>0일 때만)
            _sel_info = " ".join(
                f"{x['sym'].replace('/USDT','')}(β={x['beta']:.2f},c={x['corr_24h']:.2f}"
                + (f",p={x['pnl_score']:+.2f}" if 'pnl_score' in x else "")
                + ")"
                for x in cands[:target_n]
            )
            print(f"[Universe V10.15] {pool_name}: pool={len(pool_syms)} "
                  f"vol={len(active_syms)} filter={len(cands)} → {len(selected)}선발")
            print(f"[Universe V10.15] {pool_name} beta: {_sel_info}")
            return selected

        # ── Long / Short 파이프라인 실행 ──
        new_long = await _pipeline(
            long_pool, "LONG", LONG_MIN_CORR, LONG_BETA_MIN, LONG_BETA_MAX, UNIVERSE_LONG_N)
        new_short = await _pipeline(
            short_pool, "SHORT", SHORT_MIN_CORR, SHORT_BETA_MIN, SHORT_BETA_MAX, UNIVERSE_SHORT_N)

        if _excluded_log:
            show = _excluded_log[:8]
            extra = len(_excluded_log) - 8
            print(f"[Universe V10.15] 제외: {show}" + (f" (+{extra})" if extra > 0 else ""))

        new_short = [s for s in new_short if s not in set(new_long)]

        # ══ Step 5: Sticky 안정화 ══
        now = time.time()
        prev_long = list(getattr(snapshot, "global_targets_long", None) or [])
        prev_short = list(getattr(snapshot, "global_targets_short", None) or [])

        final_long = list(new_long)
        final_short = list(new_short)

        for sym in prev_long:
            if sym not in final_long:
                added_ts = _sticky_long.get(sym, 0)
                if added_ts > 0 and (now - added_ts) < UNIVERSE_STICKY_MIN_SEC:
                    final_long.append(sym)
        for sym in prev_short:
            if sym not in final_short:
                added_ts = _sticky_short.get(sym, 0)
                if added_ts > 0 and (now - added_ts) < UNIVERSE_STICKY_MIN_SEC:
                    final_short.append(sym)

        seen = set()
        deduped_long = []
        for s in final_long:
            if s not in seen:
                deduped_long.append(s)
                seen.add(s)
        final_long = deduped_long[:UNIVERSE_LONG_N]

        seen_s = set()
        deduped_short = []
        for s in final_short:
            if s not in seen_s and s not in set(final_long):
                deduped_short.append(s)
                seen_s.add(s)
        final_short = deduped_short[:UNIVERSE_SHORT_N]

        new_sticky_l = {sym: _sticky_long.get(sym, now) for sym in final_long}
        _sticky_long = new_sticky_l
        new_sticky_s = {sym: _sticky_short.get(sym, now) for sym in final_short}
        _sticky_short = new_sticky_s

        print(f"[Universe V10.15] Long={final_long} | Short={final_short}")

        trace_id = str(uuid.uuid4())[:8]
        from v9.strategy.planners import _btc_vol_regime as _regime_fn
        _regime_str = _regime_fn(snapshot)
        top10_syms = list(dict.fromkeys(final_long + final_short))
        log_universe(
            trace_id=trace_id, top10=top10_syms,
            long_4=final_long, short_4=final_short,
            regime=_regime_str, btc_price=snapshot.btc_price,
        )

        from dataclasses import replace
        return replace(
            snapshot,
            correlations=new_correlations,
            global_targets_long=final_long,
            global_targets_short=final_short,
        )

    except Exception as e:
        print(f"[Universe V10.15 Error] {e}")
        import traceback; traceback.print_exc()
        return snapshot
