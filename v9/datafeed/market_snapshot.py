"""
V9 Datafeed - Market Snapshot
거래소에서 실시간 시장 데이터를 수집하여 MarketSnapshot 생성
"""
import asyncio
import time

from v9.types import MarketSnapshot


async def fetch_market_snapshot(
    ex,
    active_symbols: list,
    prev_snapshot: MarketSnapshot | None = None,
    ohlcv_interval_sec: float = 10.0,
) -> MarketSnapshot:
    """
    거래소에서 전체 시장 스냅샷을 수집.
    - tickers: 전체 fetch_tickers (정규화)
    - balance: USDT 잔고 + margin_ratio
    - ohlcv_pool: active_symbols + BTC/USDT (1m/5m/15m/1h)
    - btc 지표: 1h/6h change, dev_ma
    """
    ts = time.time()

    # ── 잔고 조회 ────────────────────────────────────────────────
    real_balance = 0.0
    free_balance = 0.0
    try:
        balance = await asyncio.to_thread(ex.fetch_balance)
        real_balance = float(balance['USDT']['total'])
        free_balance = float(balance['USDT']['free'])
    except Exception as e:
        print(f"[market_snapshot] balance 조회 실패: {e}")
        if prev_snapshot:
            real_balance = prev_snapshot.real_balance_usdt
            free_balance = prev_snapshot.free_balance_usdt

    margin_ratio = 1.0 - (free_balance / real_balance) if real_balance > 0 else 0.0

    # ── 티커 조회 ────────────────────────────────────────────────
    tickers_raw = {}
    try:
        tickers_raw = await asyncio.to_thread(ex.fetch_tickers)
        if not isinstance(tickers_raw, dict):
            tickers_raw = {}
    except Exception as e:
        print(f"[market_snapshot] fetch_tickers 실패: {e}")

    norm_tickers = {}
    all_prices = {}
    all_volumes = {}

    for k, v in tickers_raw.items():
        if not isinstance(v, dict):
            continue
        norm_key = k.replace(':USDT', '') if isinstance(k, str) and k.endswith(':USDT') else k
        _last = v.get('last') or 0.0
        _ask  = v.get('ask') or _last
        _bid  = v.get('bid') or _last
        norm_tickers[norm_key] = {
            'ask': float(_ask),
            'bid': float(_bid),
            'last': float(_last),
        }
        all_prices[norm_key] = float(_last)
        all_volumes[norm_key] = float(v.get('quoteVolume', 0.0) or 0.0)

    btc_price = all_prices.get('BTC/USDT', 0.0)

    # ── OHLCV 수집 ───────────────────────────────────────────────
    ohlcv_pool = {}
    if prev_snapshot:
        ohlcv_pool = prev_snapshot.ohlcv_pool.copy()

    last_ohlcv_ts = getattr(prev_snapshot, '_last_ohlcv_ts', 0.0) if prev_snapshot else 0.0
    if ts - last_ohlcv_ts >= ohlcv_interval_sec:
        fetch_syms = set(active_symbols)
        fetch_syms.add('BTC/USDT')
        fetch_syms.discard('')

        # ★ v10.12: 병렬 OHLCV fetch (108 순차 → 배치 병렬, 21초 → 3~5초)
        _sem = asyncio.Semaphore(12)  # 동시 12건 (rate limit 안전)

        async def _fetch_sym(sym):
            async with _sem:
                try:
                    o_1m, o_5m, o_15m, o_1h = await asyncio.gather(
                        asyncio.to_thread(ex.fetch_ohlcv, sym, '1m',  limit=70),
                        asyncio.to_thread(ex.fetch_ohlcv, sym, '5m',  limit=50),
                        asyncio.to_thread(ex.fetch_ohlcv, sym, '15m', limit=85),
                        asyncio.to_thread(ex.fetch_ohlcv, sym, '1h',  limit=55),
                    )
                    return sym, {'1m': o_1m, '5m': o_5m, '15m': o_15m, '1h': o_1h}
                except Exception:
                    return sym, None

        _results = await asyncio.gather(*[_fetch_sym(s) for s in fetch_syms])
        for sym, data in _results:
            if data is not None:
                ohlcv_pool[sym] = data
        last_ohlcv_ts = ts

    # ── BTC 지표 계산 ────────────────────────────────────────────
    btc_1h_change = 0.0
    btc_6h_change = 0.0
    dev_ma = 0.0

    btc_ohlcv = ohlcv_pool.get('BTC/USDT', {}).get('1h', [])
    if len(btc_ohlcv) >= 10:
        try:
            c_now   = float(btc_ohlcv[-2][4])
            c_1h    = float(btc_ohlcv[-3][4])
            c_6h    = float(btc_ohlcv[-8][4])
            if c_1h > 0:
                btc_1h_change = (c_now - c_1h) / c_1h
            if c_6h > 0:
                btc_6h_change = (c_now - c_6h) / c_6h
            if len(btc_ohlcv) >= 21:
                ma20 = sum(float(x[4]) for x in btc_ohlcv[-21:-1]) / 20
                if ma20 > 0 and btc_price > 0:
                    dev_ma = (btc_price - ma20) / ma20 * 100
        except Exception:
            pass

    # ── 상관계수 (prev 유지) ─────────────────────────────────────
    correlations = {}
    if prev_snapshot:
        correlations = prev_snapshot.correlations.copy()

    # 펀딩비는 universe_asym_v2.py Step 2에서 직접 fetch — 여기선 불필요
    funding_map: dict = {}

    snap = MarketSnapshot(
        tickers=norm_tickers,
        all_prices=all_prices,
        all_volumes=all_volumes,
        ohlcv_pool=ohlcv_pool,
        correlations=correlations,
        all_fundings=funding_map,
        btc_price=btc_price,
        btc_1h_change=btc_1h_change,
        btc_6h_change=btc_6h_change,
        dev_ma=dev_ma,
        real_balance_usdt=real_balance,
        free_balance_usdt=free_balance,
        margin_ratio=margin_ratio,
        baseline_balance=prev_snapshot.baseline_balance if prev_snapshot else real_balance,
        global_targets_long=prev_snapshot.global_targets_long if prev_snapshot else [],
        global_targets_short=prev_snapshot.global_targets_short if prev_snapshot else [],
        timestamp=ts,
        valid=(btc_price > 0 and real_balance > 0),
    )
    # ohlcv 타임스탬프 보존 (dataclass에 없으므로 동적 속성)
    snap._last_ohlcv_ts = last_ohlcv_ts  # type: ignore
    return snap
