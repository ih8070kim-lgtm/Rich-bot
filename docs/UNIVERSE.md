# UNIVERSE — 유니버스 & 데이터

## MR 유니버스 (universe_asym_v2.py)
```
소스: 바이낸스 USDT-M 선물 전체
필터:
  - 24h 거래대금 상위
  - 상관계수 ≥ OPEN_CORR_MIN (0.60)
  - BTC/USDT 제외 (헷지/지표용으로만 사용)
갱신: 매 틱 (OHLCV 풀링과 동시)
```

## BC 유니버스
```
소스: 바이낸스 USDT-M 상위 거래대금
필터: BC_UNI_TOP_N = 20 (상위 20개)
갱신: 일 1회
```

## OHLCV 데이터
```
타임프레임: 1m, 5m, 15m, 1h, 1d
폴링: asyncio.gather 병렬 (순차 대비 ~5배 빠름)
버퍼: snapshot.ohlcv_pool = {심볼: {tf: [[ts,o,h,l,c,v], ...]}}
```

## 상관계수
```
snapshot.correlations = {심볼: float}
BTC 대비 상관계수. 0~1 범위.
진입 게이트: ≥ 0.60 (MR), ≥ 0.40 (Hedge)
```

## BTC 변동성 레짐 (_btc_vol_regime)
```
LOW:    ATR 낮음 — 횡보
NORMAL: 보통
HIGH:   ATR 높음 — 추세/변동성 큼
```
EMA 스무딩 + 히스테리시스 적용 (빈번한 전환 방지)

## 데이터 영속화
```
system_state["ohlcv_pool"]  → 매 틱 갱신 (메모리)
_btc_hourly / _btc_daily    → beta_cycle.py 내부 버퍼
_hourly_closes / _daily_closes → beta_cycle.py 심볼별 버퍼
_hourly_volumes             → beta_cycle.py 거래량 버퍼 (BC 볼륨 필터용)
```

## 수정 시 체크
- [ ] BTC/USDT가 TREND 후보에서 제외되는지
- [ ] 상관계수 필터가 OPEN/TREND 양쪽에 적용되는지
- [ ] OHLCV 병렬 풀링이 유지되는지 (순차로 바꾸면 틱 시간 5배 증가)
