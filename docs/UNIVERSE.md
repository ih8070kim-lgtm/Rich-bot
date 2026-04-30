# UNIVERSE — 유니버스 & 데이터

## ★ V10.31AO: 30분 BTC corr 진입 필터 [04-30]

### 사용자 결정 [04-30]
3시간 corr_3h가 길음 — 진입 직전 30분 BTC 상관성으로 **혼자 튀는 놈** 사전 식별.

### 데이터 발견 [실측 19일]
1초 윈도우 (같은 사이클) 동시 진입 vs 단독 진입:
- 단독 (1초 X): SL 18.2%, 평균 -$1.07
- 동시 (같은 ts): SL **3.0%**, 평균 +$1.73

→ **동시 진입(BTC 상관성 ↑) = 시그널 신뢰도 매우 높음**. 단독은 6배 SL 위험.

### 변경 내용
```python
# config.py
OPEN_CORR_MIN_30M = 0.50   # ★ V10.31AO: 30분 corr 임계 (진입 필터)

# types.py
correlations_30m: dict = field(default_factory=dict)

# universe_asym_v2.py
btc_lr_30m = log_returns(btc_1m_closes)  # BTC 1m × 30
alt_lr_30m = log_returns(alt_1m_closes)  # alt 1m × 30
corr_30m = safe_corr(btc_lr_30m, alt_lr_30m)
new_correlations_30m[sym_name] = corr_30m

# risk_manager.py — OPEN 우선순위
# 1순위: corr_30m  (30분, V10.31AO)
# 2순위: corr_3h   (3시간, V10.31AM)
# 3순위: corr_24h  (24시간, fallback)
```

### 데이터 소스
- BTC 1m × 30 = 30분 OHLCV
- alt 1m × 30 = 심볼별 30분 OHLCV
- 30개 1m 데이터 포인트 = 통계적 신뢰도 적정 (15분=15포인트는 노이즈 큼)
- log_returns 계산 후 Pearson corr

### 우선순위 매핑
- **OPEN (CORE_MR/CORE_MR_HEDGE)**:
  - corr_30m 있으면 사용 (`OPEN_CORR_MIN_30M = 0.50`)
  - corr_30m 없으면 corr_3h fallback (`OPEN_CORR_MIN = 0.50`)
  - 둘 다 없으면 corr_24h fallback
- **DCA/HEDGE**: corr_24h 그대로 (이미 진입된 포지션 장기 연결성)

### 한계 [필수 고지]
1. **데이터 없는 케이스**: alt 1m fetch 실패 시 corr_30m 저장 X → corr_3h fallback
2. **30분 = 30포인트 통계 한계**: corr 노이즈로 정상 진입까지 차단 가능
3. **임계 0.50**: 보수적 시작점. 운영 1주 후 단독 진입의 corr_30m 분포 보고 조정
4. **fetch 부담**: 모든 universe 심볼마다 1m × 30 추가 fetch — API 호출 ↑
5. **OHLCV 백테스트 없음**: 효과는 [추정]. 1주 운영 후 실측 검증 필요

### 체크리스트
- [ ] OPEN_CORR_MIN_30M = 0.50 (config.py)
- [ ] correlations_30m 필드 (types.py:121)
- [ ] correlations_30m prev_snapshot 보존 (market_snapshot.py:220)
- [ ] btc_lr_30m + corr_30m 계산 (universe_asym_v2.py:182~)
- [ ] snapshot replace에 correlations_30m 포함 (universe_asym_v2.py:441)
- [ ] risk_manager OPEN 우선순위 30m → 3h → 24h

### 롤백
config.py 임계 강화로 비활성: `OPEN_CORR_MIN_30M = -1.0` (사실상 무필터)
또는 risk_manager.py에서 corr_30m 분기 제거 → corr_3h만 사용 (V10.31AM 상태 회귀)

---

## ★ V10.31AM3 hotfix-21: β 시간축 50h → 3h + vol_ratio_5m 로그 [04-29]

### 변경
```
universe_asym_v2.py:236  _beta = corr_24h * (alt_std/btc_std)        # 50h (이전)
                       → _beta = corr_3h * (alt_std_3h/btc_std_3h)   # 3h
```
fallback: 5m 데이터 부족 시 50h β 사용 (안전 default).

### 사용자 통찰
"MR 진입은 5분 RSI인데 그 이전 50시간 베타값이 무슨 의미가 있나"  
"위아래로 튀는 건 안 들어가려고 베타 설정하는 건데 최대한 최근 껄 봐야지"

### 진짜 베타의 목적 재정의
이전 50h β = "이 알트가 평소 BTC와 얼마나 동조하나" (장기 통계)  
hf-21 3h β = "지금 이 알트가 BTC와 동조해서 튀고 있나" (단기 동조성)

이게 사용자 통찰의 정확한 답. universe selection 시점에 "지금 튀는 알트 차단" 목적 충족.

### 시간축 정합성
| 데이터 | 단위 | 봉 수 | 시간 |
|---|---|---|---|
| 진입 신호 (5m RSI) | 5분 | 1~3 | 5~15분 |
| 진입 corr 필터 (corr_3h) | 5m | 36 | 3시간 |
| **universe β (hf-21)** | **5m** | **36** | **3시간** ← corr와 정합 |
| universe corr (24h) | 1h | 50 | 50시간 (그대로) |

corr 24h는 그대로 둠 — **universe 안정성 보존** (β만 바꿈). corr까지 3h 변경하면 universe drift 너무 커짐.

### 베타 정의의 한계 (양방향 대칭)
β = corr × (alt_std / btc_std) — Pearson corr × 변동성 비율. **상승/하락 부호 무관**. 즉:
- β=1.4 의미: BTC +1% → 알트 평균 +1.4%, BTC -1% → 알트 평균 -1.4% (대칭 가정)
- 못 잡는 패턴: downside skew (하락에 더 민감). 알트 시장 흔한 비대칭
- 향후 hf-22+ 후보: down_β / up_β 분리 계산

### 04-29 손실 핵심 메커니즘 [실측]
```
TIA SHORT 04-29 01:55  β_50h=1.40  → BTC 1h=-0.06% 6h=+0.09%  → -$19
FIL SHORT 04-29 03:20  β_50h=1.37  → BTC dev_ma=+0.66 (회복)   → -$34 (활성)
OP  SHORT 04-29 03:22  β_50h=1.43  → BTC dev_ma=+0.63 (회복)   → -$22 (활성)
```

50h β 1.4 SHORT가 BTC 단기 회복 시점에 1.4배 손실 폭증 — β 양방향 대칭 + 진입 방향 BTC 추세 충돌.

3h β로 변경 시 — BTC 단기 회복 시점에 알트 동조 정도가 직접 반영 → "지금 튀는 알트" 차단 가능성 ↑.

### vol_ratio_5m (로그 전용)
1m × 5봉 alt_std / btc_std. 진입 시점 "5분간 알트가 BTC 대비 N배 튀고 있는지" 측정.

표본 5개로 베타 정의는 부족 (Pearson corr 신뢰구간 ±0.8) but **단순 std 비율은 표본 5개도 의미** — "튐 감지" 이진 판단 목적엔 충분.

**1주 누적 후 임계 결정**:
- vol_ratio_5m × close PnL 분포로 임계 결정
- 예상 임계 후보: 1.5 / 2.0 / 2.5 / 3.0
- 결정 후 hf-22로 진입 게이트 도입

### 위험 [필수 인지]
1. **임계 미조정 위험**: LONG_BETA_MIN=0.80, LONG_BETA_MAX=2.00, SHORT_BETA_MIN=0.50, SHORT_BETA_MAX=2.00 모두 50h β 분포 fit. 3h β 분포는 self-noise ↑ → universe 사이즈 변동 가능
2. **universe drift ↑**: β 매 1시간 selection마다 변동 ↑ → 같은 sym 들어왔다 나갔다 가능
3. **활성 포지션 영향 0** [실측 코드 검증]: universe targets는 plan_open만 사용, DCA/TRIM/SL 별도 로직
4. **봇 자해 위험 0**: universe 비어도 진입 0 (정지 아님)

### 모니터링 (1~2일 후 결정)
- (i) `[Universe V10.15] LONG/SHORT beta:` 로그에서 β 값 분포 확인
- (ii) universe 사이즈 (LONG 4~5, SHORT 4~6) 정상 범위 유지
- (iii) log_btc_context.csv `universe_beta` 컬럼 분포 분석
- (iv) β_3h × close PnL 매칭 — 50h β 분석 결과보다 익절 예측력 높은지

### 향후 후보 (1주 데이터 누적 후)
- **임계 조정**: 3h β 분포 따라 LONG_BETA_MAX/SHORT_BETA_MIN 재조정
- **down_β / up_β 분리**: 비대칭 베타 도입 (BTC 상승 구간 / 하락 구간 분리 계산)
- **vol_ratio_5m 진입 게이트**: 임계 결정 후 hf-22 도입
- **β 시간축 + vol_ratio 결합**: β로 universe selection + vol_ratio로 진입 직전 게이트 = 다중 시간축 알파

### 롤백
universe_asym_v2.py에서 `if _beta_3h is not None: _beta = _beta_3h else: <fallback>` 블록 제거 → 50h β 단일 사용 복원.

---

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
