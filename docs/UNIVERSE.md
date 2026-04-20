# UNIVERSE — 유니버스 & 데이터

## ★ V10.31h: MAJOR_UNIVERSE 재구성 (29개)

```
LONG_ONLY  (4): BNB, XRP, XLM, AVAX  — 200회 100% LONG 일관 + 메이저
SHORT_ONLY (6): TIA, FET, OP, INJ, WLD, FIL  — 100% SHORT 일관 + 토크노믹스 명확
NEUTRAL   (19): ETH, SOL, LINK, ADA, DOT, SUI, APT, NEAR, ATOM, UNI,
                ARB, SEI, LDO, PENDLE, JUP, JTO, ARKM, GMX, ORDI

제거 14개 (200회 universe 갱신 0회 등장 = corr/beta filter 상시 컷):
  TRX, TON, ICP, ETC, AAVE, STX, MATIC, EOS,
  STRK, RUNE, RNDR, AGIX, AKT, GRT
신규 추가 7개 (DeFi/Solana/AI/BTC eco):
  LDO, PENDLE, JUP, JTO, ARKM, GMX, ORDI
```

### 분류 의미
- **LONG_ONLY/SHORT_ONLY**: universe_asym_v2 Step 2 풀 분리 시 강제 화이트리스트
  (펀딩비 자연 분리와 일치하는 안정 심볼만 강제 — 시나리오 A: 보수적 축소)
- **NEUTRAL**: 펀딩비 자연 분리에 따라 long_pool/short_pool 둘 다 진입 가능
- 같은 심볼이 long_pool과 short_pool 모두에 들어갈 수 있어 MR 양방향 평가 가능

## MR 유니버스 (universe_asym_v2.py)
```
소스: 바이낸스 USDT-M 선물 전체 (MAJOR_UNIVERSE 29개로 1차 필터)
필터:
  - 24h 거래대금 ≥ UNIVERSE_VOL_FLOOR_USD (500K)
  - 펀딩비 분리 (낮음 → long_pool, 높음 → short_pool)
  - LONG_ONLY/SHORT_ONLY 강제 화이트리스트 적용
  - corr ≥ MIN_CORR (long 0.50 / short 0.40)
  - beta ∈ [BETA_MIN, BETA_MAX]
  - ATR pct 기반 랭킹 → top N 선발 (long 8 / short 8)
  - PnL score tiebreaker (V10.31e — SYMBOL_PNL_WEIGHT=0.2)
갱신: 5분 sticky (UNIVERSE_STICKY_MIN_SEC=600s)
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
주의: ohlcv_pool은 봉 개수만 정리되고 심볼은 유지 → universe에서 빠져도 stale 데이터 잔존
      TREND가 이 잔존을 보고 universe 밖 진입할 수 있음 (V10.31h 인식)
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
- [ ] LONG_ONLY/SHORT_ONLY/NEUTRAL 3집합 중복 0건 (V10.31h 검증 완료)
- [ ] MAJOR_UNIVERSE = 합집합으로 자동 계산 (수동 정의 금지)
