"""
V9 Datafeed — Universe ASYM v2  (v9.2)
========================================
v9.0 → v9.1: Sticky 안정화 (신규 편입 10분 유지)
v9.1 → v9.2: 펀딩비 기반 Long/Short 풀 분리
  - fetch_funding_rates() 로 전체 펀딩비 수집
  - 거래량 통과 코인들을 펀딩비 기준으로 줄 세워 반반 분리
    · Long pool  = 펀딩비 하위 50% (비용 유리)
    · Short pool = 펀딩비 상위 50% (수취 유리)
  - 각 pool 독립적으로 BTC상관 → ATR 상위 1개 제외 → capture ratio 정렬
  - 펀딩비 fetch 실패 시 전체 풀 폴백 (안전)
  - ATR 상위 1개 제외 (기존 2 → 1)
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
    UNIVERSE_MIN_CORR,
    UNIVERSE_CORR_WHITELIST,
    UNIVERSE_SHORT_N,
    UNIVERSE_STICKY_MIN_SEC,
    UNIVERSE_TOP_N,
    UNIVERSE_VOL_MIN_USD,
)
from v9.logging.logger_csv import log_universe
from v9.types import MarketSnapshot
from v9.utils.utils_math import atr_from_ohlcv, log_returns, safe_corr

# ★ Sticky 상태 (모듈 레벨 — 프로세스 수명 동안 유지)
_sticky_long: dict = {}    # {sym: first_added_ts}
_sticky_short: dict = {}

# ★ v10.11b: 허스트 지수 — MR 적합 종목 필터
HURST_BOOST_THRESH = 0.70    # H < 0.70 → MR 유리 → ATR 스코어 ×1.2
HURST_PENALTY_THRESH = 0.85  # H >= 0.85 → 추세 종목 → ATR 스코어 ×0.7

def _calc_hurst(closes: np.ndarray, max_lag: int = 20) -> float:
    """R/S 방법 허스트 지수. H<0.5=평균회귀, H>0.5=추세.
    데이터 부족 시 0.5 반환 (중립).
    """
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
    """fetch_funding_rates() 결과를 {sym/USDT: float} 형태로 정규화."""
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


async def update_universe(
    ex,
    snapshot: MarketSnapshot,
) -> MarketSnapshot:
    """
    ASYM Universe v2.2 — 펀딩비 기반 Long/Short 풀 분리 + Sticky 안정화.

    흐름:
      1. 거래량 필터 (UNIVERSE_VOL_MIN_USD 이상)
      2. 펀딩비 fetch → 반반 분리
         Long pool  = 펀딩비 하위 50%
         Short pool = 펀딩비 상위 50%
         (fetch 실패 시 전체 풀 폴백)
      3. 각 pool 독립: BTC 상관 [MIN_CORR, MAX_CORR]
      4. ATR 내림차순 → 상위 UNIVERSE_EXCLUDE_TOP_ATR(1)개 제외
      5. TOP_N(16)개 컷
      6. capture ratio → Long/Short 각 UNIVERSE_LONG_N/SHORT_N(8)개 선발
      7. Sticky 안정화 (10분 미만 코인 강제 유지)
    """
    global _sticky_long, _sticky_short

    try:
        tickers = await asyncio.to_thread(ex.fetch_tickers)

        # ── Step 1: 거래량 필터 ───────────────────────────────────
        vol_list = []
        for sym in MAJOR_UNIVERSE:
            if sym in GLOBAL_BLACKLIST:
                continue
            ticker_key = f"{sym}:USDT" if f"{sym}:USDT" in tickers else sym
            if ticker_key not in tickers:
                continue
            vol = float(tickers[ticker_key].get("quoteVolume", 0) or 0)
            if vol >= UNIVERSE_VOL_MIN_USD:
                vol_list.append({"sym": sym, "vol": vol})

        if not vol_list:
            print("[Universe V9.2] vol_list empty — skip")
            return snapshot

        vol_list.sort(key=lambda x: x["vol"], reverse=True)
        vol_filtered_syms = [x["sym"] for x in vol_list]

        # ── Step 2: 펀딩비 fetch → 반반 풀 분리 ─────────────────
        # ★ v10.8: 베타 필터 — 0.8 < β < 2.0 범위 밖 코인 제외
        # β = corr × (σ_coin / σ_BTC) — BTC 대비 가격 민감도
        # β > 2: BTC 2배 이상 증폭 → 숏 위험 / β < 0.8: BTC 미추종 → MR 부적합
        BETA_MIN = 0.8
        BETA_MAX = 2.0

        funding_map: dict = {}
        try:
            raw_fr = await asyncio.to_thread(ex.fetch_funding_rates)
            funding_map = _fetch_funding_map(raw_fr)
        except Exception as fe:
            print(f"[Universe V9.2] 펀딩비 fetch 실패 (전체 풀 폴백): {fe}")

        if funding_map:
            fr_sorted = sorted(
                vol_filtered_syms,
                key=lambda s: funding_map.get(s, 0.0)
            )
            mid = len(fr_sorted) // 2
            long_pool_syms  = set(fr_sorted[:mid])   # 펀딩비 하위 50%
            short_pool_syms = set(fr_sorted[mid:])   # 펀딩비 상위 50%
            print(
                f"[Universe V9.2] 펀딩비 분리 total={len(fr_sorted)} "
                f"mid={mid} | Long={sorted(long_pool_syms)} | Short={sorted(short_pool_syms)}"
            )
        else:
            long_pool_syms  = set(vol_filtered_syms)
            short_pool_syms = set(vol_filtered_syms)

        # ★ v10.5: 심볼 바이어스 반영 — LONG_ONLY는 long_pool에만, SHORT_ONLY는 short_pool에만
        from v9.config import LONG_ONLY_SYMBOLS, SHORT_ONLY_SYMBOLS
        long_pool_syms  = (long_pool_syms  - SHORT_ONLY_SYMBOLS) | (long_pool_syms  & LONG_ONLY_SYMBOLS)
        short_pool_syms = (short_pool_syms - LONG_ONLY_SYMBOLS)  | (short_pool_syms & SHORT_ONLY_SYMBOLS)
        # LONG_ONLY가 short_pool에만 있던 경우 → long_pool로 강제 이동
        for sym in LONG_ONLY_SYMBOLS:
            if sym in vol_filtered_syms and sym not in long_pool_syms:
                long_pool_syms.add(sym)
                short_pool_syms.discard(sym)
        # SHORT_ONLY가 long_pool에만 있던 경우 → short_pool로 강제 이동
        for sym in SHORT_ONLY_SYMBOLS:
            if sym in vol_filtered_syms and sym not in short_pool_syms:
                short_pool_syms.add(sym)
                long_pool_syms.discard(sym)
        print(
            f"[Universe V9.2] 바이어스 적용 후 | Long={sorted(long_pool_syms)} | Short={sorted(short_pool_syms)}"
        )

        # ── BTC 1h OHLCV ─────────────────────────────────────────
        try:
            btc_ohlcv = await asyncio.to_thread(
                ex.fetch_ohlcv, "BTC/USDT", "1h", limit=50,
            )
            if len(btc_ohlcv) < 24:
                print("[Universe V9.2] BTC OHLCV insufficient — skip")
                return snapshot
            btc_closes = np.array([float(x[4]) for x in btc_ohlcv])
            btc_lr = log_returns(btc_closes)
        except Exception as e:
            print(f"[Universe V9.2] BTC OHLCV error: {e}")
            return snapshot

        # ── Step 3~4: BTC상관 + ATR 수집 (pool별 독립) ───────────
        new_correlations = dict(snapshot.correlations)

        # ★ v10.8: BTC 표준편차 (베타 계산용)
        _btc_std = float(np.std(btc_lr)) if len(btc_lr) > 1 else 1.0
        _beta_excluded = []
        _hurst_penalized = []

        async def build_candidates(pool_syms: set, pool_name: str = "") -> list:
            cands = []
            for sym in vol_filtered_syms:
                if sym not in pool_syms:
                    continue
                try:
                    alt_ohlcv = await asyncio.to_thread(
                        ex.fetch_ohlcv, sym, "1h", limit=50,
                    )
                    if len(alt_ohlcv) < 24:
                        continue

                    alt_closes = np.array([float(x[4]) for x in alt_ohlcv])
                    alt_lr = log_returns(alt_closes)
                    corr_24h = safe_corr(btc_lr, alt_lr)
                    new_correlations[sym] = corr_24h

                    if corr_24h < UNIVERSE_MIN_CORR:
                        continue
                    if corr_24h > UNIVERSE_MAX_CORR and sym not in UNIVERSE_CORR_WHITELIST:
                        continue

                    # ★ v10.8: 베타 계산 — β = corr × (σ_coin / σ_BTC)
                    _alt_std = float(np.std(alt_lr)) if len(alt_lr) > 1 else 0.0
                    _beta = corr_24h * (_alt_std / _btc_std) if _btc_std > 0 else 1.0
                    if _beta < BETA_MIN or _beta > BETA_MAX:
                        _beta_excluded.append(f"{sym}(β={_beta:.2f},{pool_name})")
                        continue

                    atr_val = atr_from_ohlcv(alt_ohlcv, period=10)
                    curr_price = float(alt_ohlcv[-1][4])
                    if curr_price <= 0:
                        continue
                    atr_pct = atr_val / curr_price if atr_val > 0 else 0.0

                    # ★ v10.11b: 허스트 소프트 필터 — 제거 없이 랭킹 조절
                    _hurst = _calc_hurst(alt_closes)
                    if _hurst < HURST_BOOST_THRESH:
                        _h_mult = 1.2   # MR 유리 → 우대
                    elif _hurst >= HURST_PENALTY_THRESH:
                        _h_mult = 0.7   # 강한 추세 → 후순위
                        _hurst_penalized.append(f"{sym}(H={_hurst:.2f},{pool_name})")
                    else:
                        _h_mult = 1.0
                    _atr_score = atr_pct * _h_mult

                    cands.append({
                        "sym": sym,
                        "corr_24h": corr_24h,
                        "atr_pct": _atr_score,
                        "beta": _beta,
                        "hurst": _hurst,
                    })
                    await asyncio.sleep(0.05)
                except Exception:
                    continue
            return cands

        long_cands  = await build_candidates(long_pool_syms, "L")
        short_cands = await build_candidates(short_pool_syms, "S")

        if _beta_excluded:
            print(f"[Universe V10.8] 베타 제외 ({BETA_MIN}<β<{BETA_MAX}): {_beta_excluded}")
        if _hurst_penalized:
            print(f"[Universe V10.11b] 허스트 패널티 (H≥{HURST_PENALTY_THRESH}, ×0.7): {_hurst_penalized}")

        # ── Step 4: ATR 상위 1개 제외 ────────────────────────────
        long_cands.sort(key=lambda x: x["atr_pct"], reverse=True)
        long_after_atr  = long_cands[UNIVERSE_EXCLUDE_TOP_ATR:]

        short_cands.sort(key=lambda x: x["atr_pct"], reverse=True)
        short_after_atr = short_cands[UNIVERSE_EXCLUDE_TOP_ATR:]

        print(
            f"[Universe V9.2] Long pool: vol={len(long_pool_syms)} "
            f"corr={len(long_cands)} atr제외={len(long_after_atr)} | "
            f"Short pool: vol={len(short_pool_syms)} "
            f"corr={len(short_cands)} atr제외={len(short_after_atr)}"
        )

        # 모수 부족 체크 (한쪽이라도 0이면 스킵)
        if len(long_after_atr) == 0 or len(short_after_atr) == 0:
            print("[Universe V9.2] 후보 부족 — 갱신 스킵")
            from dataclasses import replace
            return replace(snapshot, correlations=new_correlations)

        # ── Step 5: TOP_N 컷 ─────────────────────────────────────
        long_top  = long_after_atr[:UNIVERSE_TOP_N]
        short_top = short_after_atr[:UNIVERSE_TOP_N]

        # ── Step 6: ATR% 순위 → Long/Short 선발 ──────────────────
        # ★ v10.6: capture ratio(추세추종 지표) → ATR%(MR 적합도)로 교체
        # 이미 ATR desc 정렬 상태이므로 그대로 상위 N개 선발
        new_long  = [x["sym"] for x in long_top[:UNIVERSE_LONG_N]]
        new_short = [x["sym"] for x in short_top[:UNIVERSE_SHORT_N]]

        # Long/Short 중복 제거 (Long 우선)
        new_short = [s for s in new_short if s not in set(new_long)]

        # 로그용 top 리스트
        top10_syms = list(dict.fromkeys(new_long + new_short))

        # ── Step 7: Sticky 안정화 ────────────────────────────────
        now = time.time()
        prev_long  = list(getattr(snapshot, "global_targets_long",  None) or [])
        prev_short = list(getattr(snapshot, "global_targets_short", None) or [])

        final_long  = list(new_long)
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

        # 중복 제거 + 슬롯 수 제한
        seen: set = set()
        deduped_long = []
        for s in final_long:
            if s not in seen:
                deduped_long.append(s)
                seen.add(s)
        final_long = deduped_long[:UNIVERSE_LONG_N]

        seen_s: set = set()
        deduped_short = []
        for s in final_short:
            if s not in seen_s and s not in set(final_long):
                deduped_short.append(s)
                seen_s.add(s)
        final_short = deduped_short[:UNIVERSE_SHORT_N]

        # Sticky 타임스탬프 갱신
        new_sticky_l = {}
        for sym in final_long:
            new_sticky_l[sym] = _sticky_long.get(sym, now)
        _sticky_long = new_sticky_l

        new_sticky_s = {}
        for sym in final_short:
            new_sticky_s[sym] = _sticky_short.get(sym, now)
        _sticky_short = new_sticky_s

        print(f"[Universe V9.2] Long={final_long} | Short={final_short}")

        # ── CSV 로그 ──────────────────────────────────────────────
        trace_id = str(uuid.uuid4())[:8]
        from v9.strategy.planners import _btc_vol_regime as _regime_fn
        _regime_str = _regime_fn(snapshot)
        log_universe(
            trace_id=trace_id,
            top10=top10_syms,
            long_4=final_long,
            short_4=final_short,
            regime=_regime_str,
            btc_price=snapshot.btc_price,
        )

        from dataclasses import replace
        return replace(
            snapshot,
            correlations=new_correlations,
            global_targets_long=final_long,
            global_targets_short=final_short,
        )

    except Exception as e:
        print(f"[Universe V9.2 Error] {e}")
        return snapshot
