"""
V9 Datafeed - Market Snapshot
거래소에서 실시간 시장 데이터를 수집하여 MarketSnapshot 생성
"""
import asyncio
import time

from v9.types import MarketSnapshot


# ★ V10.31c: Balance 캐시 (15초) — 429 rate limit 방어
# fetch_balance는 매 tick(3초) 호출 불필요. 15초 캐시로 1/5 감축.
_BAL_CACHE = {"ts": 0.0, "real": 0.0, "free": 0.0}
_BAL_CACHE_TTL = 15.0

# ★ V10.31e-3: Tickers 캐시 (초기 3s → V10.31e-5: 5s) — 418 IP 밴 대응
# 04-20 ~04:40 KST 418 I'm a teapot 발생 (550분 밴). 기존 3s로도 부족 → 5s로 확대.
# fetch_tickers weight 40 × 분당 12회 = 480 weight/분 (기존 800).
# 5초 지연은 5m 봉 전략에 무해.
_TICKERS_CACHE = {"ts": 0.0, "data": None}
_TICKERS_CACHE_TTL = 5.0


async def fetch_market_snapshot(
    ex,
    active_symbols: list,
    prev_snapshot: MarketSnapshot | None = None,
    ohlcv_interval_sec: float = 30.0,  # ★ V10.31e-5: 15→30s, 418 밴 대응 (weight 1280→640)
) -> MarketSnapshot:
    """
    거래소에서 전체 시장 스냅샷을 수집.
    - tickers: 전체 fetch_tickers (정규화)
    - balance: USDT 잔고 + margin_ratio (★ V10.31c: 15초 캐시)
    - ohlcv_pool: active_symbols + BTC/USDT (1m/5m/15m/1h)
    - btc 지표: 1h/6h change, dev_ma
    """
    ts = time.time()

    # ── 잔고 조회 ────────────────────────────────────────────────
    # ★ V10.31c: 15초 캐시 + 실패 시 prev fallback (기존 동작 유지)
    real_balance = 0.0
    free_balance = 0.0
    _cache_age = ts - _BAL_CACHE["ts"]
    if _cache_age < _BAL_CACHE_TTL and _BAL_CACHE["real"] > 0:
        # 캐시 히트
        real_balance = _BAL_CACHE["real"]
        free_balance = _BAL_CACHE["free"]
    else:
        # 캐시 만료 → API 호출
        try:
            balance = await asyncio.to_thread(ex.fetch_balance)
            real_balance = float(balance['USDT']['total'])
            free_balance = float(balance['USDT']['free'])
            _BAL_CACHE["ts"] = ts
            _BAL_CACHE["real"] = real_balance
            _BAL_CACHE["free"] = free_balance
        except Exception as e:
            print(f"[market_snapshot] balance 조회 실패: {e}")
            # 실패 시 stale 캐시 우선, 없으면 prev snapshot
            if _BAL_CACHE["real"] > 0:
                real_balance = _BAL_CACHE["real"]
                free_balance = _BAL_CACHE["free"]
            elif prev_snapshot:
                real_balance = prev_snapshot.real_balance_usdt
                free_balance = prev_snapshot.free_balance_usdt

    margin_ratio = 1.0 - (free_balance / real_balance) if real_balance > 0 else 0.0

    # ── 티커 조회 ────────────────────────────────────────────────
    # ★ V10.31e-3: 3초 캐시로 429 rate limit 방어
    tickers_raw = {}
    _tk_age = ts - _TICKERS_CACHE["ts"]
    if _tk_age < _TICKERS_CACHE_TTL and _TICKERS_CACHE["data"]:
        tickers_raw = _TICKERS_CACHE["data"]
    else:
        try:
            tickers_raw = await asyncio.to_thread(ex.fetch_tickers)
            if not isinstance(tickers_raw, dict):
                tickers_raw = {}
            else:
                _TICKERS_CACHE["ts"] = ts
                _TICKERS_CACHE["data"] = tickers_raw
        except Exception as e:
            _es = str(e)
            # ★ V10.31e-5: 418 IP 밴 감지 → 밴 해제 ts 파싱 후 글로벌 신호
            # runner 메인 루프에서 read해서 장시간 슬립 처리 (폭주 재발 방지).
            if "418" in _es or "banned until" in _es.lower():
                import re
                _m = re.search(r'banned until (\d+)', _es)
                if _m:
                    _unban_ms = int(_m.group(1))
                    import os
                    # 파일로 플래그 저장 (간단하고 재시작 시에도 인지)
                    try:
                        _flag_path = "/tmp/trinity_ban_until.txt"
                        with open(_flag_path, 'w') as _f:
                            _f.write(str(_unban_ms))
                        _wait_sec = max(0, (_unban_ms / 1000) - time.time())
                        print(f"[CRITICAL] 418 IP 밴 감지. 해제 ts={_unban_ms}, 남은 {_wait_sec/60:.0f}분. "
                              f"플래그 파일: {_flag_path}", flush=True)
                    except Exception:
                        pass
            print(f"[market_snapshot] fetch_tickers 실패: {e}")
            # ★ V10.31c: tickers 실패 시 prev snapshot에서 tickers 복원 (아래 norm_tickers 생성부에서 fallback)
            # ★ V10.31e-3: 캐시도 fallback 소스
            if _TICKERS_CACHE["data"]:
                tickers_raw = _TICKERS_CACHE["data"]
            elif prev_snapshot and hasattr(prev_snapshot, 'tickers'):
                tickers_raw = prev_snapshot.tickers or {}

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
                        asyncio.to_thread(ex.fetch_ohlcv, sym, '1h',  limit=60),
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
    btc_10m_change = 0.0  # ★ V14.17 [05-13]: 10분 변화율 (5m OHLCV 2개 비교)
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
    
    # ★ V14.17: BTC 10m 변화율 — 5m OHLCV 2개 (10분) 비교
    btc_5m_ohlcv = ohlcv_pool.get('BTC/USDT', {}).get('5m', [])
    if len(btc_5m_ohlcv) >= 3:
        try:
            c_now_5m = float(btc_5m_ohlcv[-2][4])  # 직전 완성봉
            c_10m_ago = float(btc_5m_ohlcv[-4][4])  # 10분 전 봉
            if c_10m_ago > 0:
                btc_10m_change = (c_now_5m - c_10m_ago) / c_10m_ago
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
        btc_10m_change=btc_10m_change,  # ★ V14.17
        dev_ma=dev_ma,
        real_balance_usdt=real_balance,
        free_balance_usdt=free_balance,
        margin_ratio=margin_ratio,
        baseline_balance=prev_snapshot.baseline_balance if prev_snapshot else real_balance,
        global_targets_long=prev_snapshot.global_targets_long if prev_snapshot else [],
        global_targets_short=prev_snapshot.global_targets_short if prev_snapshot else [],
        # ★ V10.31AM3 hotfix-21b: prev_snapshot에서 beta_by_sym/correlations_3h 보존
        #   배경: V10.31q에서 beta_by_sym 필드 추가됐으나 build_snapshot 인자에서 누락 → 매 루프 빈 dict 초기화
        #         hf-21에서 universe_beta 로깅 시도했으나 진입 시점엔 빈 dict라 100% 0.0 기록
        #   해결: global_targets_long/short와 동일 패턴으로 prev_snapshot에서 보존
        #   correlations_3h도 V10.31AM에서 동일 누락 — 함께 fix
        beta_by_sym=getattr(prev_snapshot, 'beta_by_sym', {}) if prev_snapshot else {},
        correlations_3h=getattr(prev_snapshot, 'correlations_3h', {}) if prev_snapshot else {},
        # ★ V10.31AO: correlations_30m도 동일 패턴 보존
        correlations_30m=getattr(prev_snapshot, 'correlations_30m', {}) if prev_snapshot else {},
        timestamp=ts,
        valid=(btc_price > 0 and real_balance > 0),
    )
    # ohlcv 타임스탬프 보존 (dataclass에 없으므로 동적 속성)
    snap._last_ohlcv_ts = last_ohlcv_ts  # type: ignore
    return snap
