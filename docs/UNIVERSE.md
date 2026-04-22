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
      ★ V10.31q fix: TREND_NOSLOT/COMPANION에 universe 필터 추가 (side별 allowed_pool 체크)
      → 과거 universe 심볼의 stale ohlcv로 진입하는 케이스 원천 차단
      (LINK 04-22 04:42 케이스 — 3h51m 전 제외된 심볼 진입 실측)
```

## Beta 계산 & 로그 (★ V10.31q)
```
계산: universe_asym_v2._pipeline — _beta = corr_24h × (alt_std / btc_std)
      (24h 상관관계 × 변동성 비율)
필터: LONG  BETA [0.80, 2.00]
      SHORT BETA [0.50, 2.00]
Snapshot 저장: MarketSnapshot.beta_by_sym: dict[str, float]
               {"LINK/USDT": 1.45, "OP/USDT": 1.20, ...}
Log:
  log_system.csv (universe 갱신 시):
    UNIV_LONG_BETA  LINK:β1.45/c0.86 ETH:β1.20/c0.92 ...
    UNIV_SHORT_BETA OP:β1.35/c0.72 ...
  log_system.csv (TREND_NOSLOT 발사 시):
    TREND_NOSLOT  LINK/USDT buy score=2.7 corr=0.86 β=1.45 FIRE
이력 분석: β 값과 실제 손익 상관관계 추적 가능 (후행지표 실전 검증용)
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
- [ ] ★ V10.31q: _pipeline이 `(selected, beta_dict)` 튜플 반환 (호출부 2곳 unpacking)
- [ ] ★ V10.31q: snapshot.beta_by_sym 필드가 MarketSnapshot dataclass에 정의되어 있는지
- [ ] ★ V10.31q: replace()에 `beta_by_sym=_combined_betas` 포함되어 있는지
- [ ] ★ V10.31q: TREND_NOSLOT/COMP 루프가 `_tr_allowed_pool`로 universe 필터링하는지

## V10.31w: LONG/SHORT ONLY 전용 심볼 보호 — 3중 방어

실측 위반 케이스:
- 04-22 05:17 FIL/USDT buy TREND_NOSLOT (FIL은 SHORT_ONLY)
- 04-22 12:45 XRP/USDT sell TREND_NOSLOT (XRP는 LONG_ONLY)
- 04-22 20:22 XRP/USDT sell HEDGE_COMP (XRP는 LONG_ONLY)

근본 원인: universe sticky 로직이 전용심볼 필터 우회
  → 과거 universe에 있던 심볼이 sticky 시간 내 유지
  → 그 후 SHORT_ONLY/LONG_ONLY 규칙 위반해도 계속 pool에 있음
  → TREND_NOSLOT/HEDGE_COMP가 pool 체크 통과 후 위반 진입

방어선 (다중 체크):
1. universe_asym_v2 sticky 재진입 시 LONG/SHORT ONLY 필터 적용 (근본 수정)
2. TREND_NOSLOT 최종 발사 직전 NOSLOT_WHITELIST_BLOCK 체크
3. HEDGE_COMP 발사 조건 추가 (V10.31w 이전부터 있었음)

로그:
- `NOSLOT_WHITELIST_BLOCK`: NOSLOT 발사 차단 (sym, side, sig_sym, 이유)
- `HEDGE_SKIP_WHITELIST`: HEDGE_COMP 발사 차단 (sym, side, MR 반대)
