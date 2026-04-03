# Trinity V10.26 — System Architecture

## 1. 시스템 개요

Binance USDT-M 선물 자동 매매 봇. Mean Reversion(MR) 전략 기반, 4단 DCA + 부분 익절 + 스큐 관리.

```
잔고: ~$2,000 USDT  |  레버리지: 3x  |  최대 슬롯: 10 (롱5/숏5)
틱 주기: ~3초  |  자산: BTC 상관 알트코인 30종+  |  거래소: Binance Hedge Mode
```

---

## 2. 모듈 구조

```
v9/
├── app/
│   └── runner.py (1935L)         # 메인 루프 + pending limit 관리 + TP1 선주문
├── strategy/
│   ├── planners.py (2545L)       # 전략 엔진 (8개 플래너 + 스큐 조절)
│   └── strategy_core.py (361L)   # 주문 결과 → 포지션북 반영
├── engines/
│   ├── hedge_core.py (534L)      # CORE_HEDGE 진입/관리/스큐 계산
│   ├── hedge_engine_v2.py (140L) # 레거시 SOFT_HEDGE exit
│   ├── hedge_engine.py (313L)    # 레거시 HEDGE 로직
│   └── dca_engine.py (19L)       # DCA → planners.plan_dca 위임
├── execution/
│   ├── execution_engine.py (218L)# Intent 우선순위 정렬 + 실행
│   ├── order_router.py (339L)    # limit/market 라우팅 + DEDUP + pending registry
│   └── position_book.py (307L)   # 포지션 상태 저장/로드/조회
├── risk/
│   ├── risk_manager.py (356L)    # 7단계 리스크 게이트 (R0~R7)
│   ├── slot_manager.py (177L)    # 슬롯 카운트 + 동적 한도
│   └── exposure.py (156L)        # 방향별 노출 계산
├── datafeed/
│   ├── market_snapshot.py (156L) # 시장 데이터 수집 (가격/OHLCV/MR)
│   └── universe_asym_v2.py (313L)# 유니버스 선정 (롱/숏 분리)
├── config.py (276L)              # 전체 파라미터
├── types.py (146L)               # Intent, OrderResult, MarketSnapshot 등
├── utils/
│   ├── utils_math.py (186L)      # RSI, EMA, ATR, ROI 계산
│   └── utils_time.py              # 시간 유틸리티
└── logging/
    ├── logger_csv.py (310L)      # CSV 로깅 (intents/fills/trades/risk/positions)
    └── logger_ml.py (211L)       # ML 피처 로깅

telegram_engine.py (400L)         # 텔레그램 알림 (체결/상태)
telegram_bot.py                   # 텔레그램 명령 (/close_all, /status 등)
ai_engine.py (587L)               # LLM 감성분석 + DCA 승인 (현재 보조)
```

---

## 3. 메인 루프 (runner.py)

```
┌─────────────────────────────────────────────────────┐
│  _main_loop (매 ~3초)                                │
├─────────────────────────────────────────────────────┤
│  1. config_override.json 핫리로드                     │
│  2. 텔레그램 명령 싱크 (close_all 등)                  │
│  3. 유니버스 갱신 (10분 주기)                          │
│  4. fetch_market_snapshot (가격/OHLCV/MR/상관성)       │
│  5. max_roi / worst_roi 매틱 갱신                     │
│  6. RECOVERED 좀비 청산 (30분 경과 시)                  │
│  7. generate_all_intents (8개 플래너)                  │
│  8. generate_corrguard_intents (CorrGuard)            │
│  9. evaluate_intent (7단 리스크 게이트)                 │
│ 10. execute_intents (우선순위 정렬 → 거래소 주문)       │
│ 11. apply_order_results (포지션북 반영)                 │
│ 12. _sync_positions_with_exchange (거래소 대조)         │
│ 13. _manage_pending_limits (limit 체결 추적)           │
│ 14. _manage_tp1_preorders (TP1 선주문 관리)            │
│ 15. 유령 pending_entry 정리                           │
│ 16. save_position_book (JSON 저장)                    │
│ 17. heartbeat + system_state 기록                     │
└─────────────────────────────────────────────────────┘
```

---

## 4. Intent 생성 파이프라인 (planners.py)

### 4.1 실행 순서 (P0 → P5)

```
┌──────────────────────────────────────────────────────────────┐
│  generate_all_intents                                        │
├──────────────────────────────────────────────────────────────┤
│  P0  plan_force_close    HARD_SL / ZOMBIE / INS_TIMECUT      │
│  P0  plan_pair_cut       스큐 페어 컷 (heavy+light 동시)      │
│  P1  plan_hedge_core_manage  CORE_HEDGE → MR 전환/trailing    │
│  P2  plan_tp1            TP1 부분익절 (Rebound Alpha)         │
│  P3  plan_trail_on       Trailing Stop                       │
│  P4  plan_dca            DCA T2/T3/T4                        │
│  P4  plan_insurance_sh   BTC_CRASH/KILLSWITCH 보험            │
│  P5  plan_open           MR 신규 진입 + HEDGE_CORE + ASYM     │
└──────────────────────────────────────────────────────────────┘
         ↓
   execute_intents (FORCE_CLOSE > TP1 > TRAIL > DCA > OPEN)
```

### 4.2 plan_open — 신규 진입

```
유니버스 심볼 루프:
  ├── 방향 필터 (LONG_ONLY / SHORT_ONLY / NEUTRAL)
  ├── 상관성 게이트 (corr ≥ 0.50)
  ├── 슬롯 확인 (동적 2→3→4)
  ├── BTC Crash 필터 (1분 -0.5% / 3분 -0.8% → 롱 차단)
  │
  ├── MR 조건 (주 엔진, 60% 승률):
  │     15m EMA 기반 ATR 거리 + 5m RSI14 과매수/과매도 + 1m 마이크로 모멘텀
  │
  ├── E30 조건 (스큐 15%+ 긴급만):
  │     5m EMA30 거리(ATR×1.4~2.0) + RSI<40/RSI>60 + 마이크로
  │
  ├── Falling Knife 필터 (5m 급락 차단)
  ├── 5m NextBar 대기 (armed → 다음 봉 시작 시 발화)
  │
  └── ASYM_FORCE (슬롯 불균형 복구):
        T2+ 발생 → 반대방향 부족 → 3분 대기 → 모멘텀 확인 → OPEN
```

### 4.3 plan_dca — DCA 4단

```
V10.26 수치:
  T2: ROI ≤ -5.0%  (쿨다운 10분)
  T3: ROI ≤ -6.5%  (쿨다운 5분)
  T4: ROI ≤ -8.0%  (쿨다운 2분)

비중: [15%, 20%, 30%, 35%]  누적: 15/35/65/100

가드:
  ├── CORE_HEDGE 수익 중 → DCA 금지
  ├── Rule A: 반대=0 AND 같은방향≥3 → 차단
  ├── Rule B (V10.26): 스큐 비례 light DCA 제한
  │     10~15% → T3 상한
  │     15%+   → T2 상한
  │     20%+   → 완전 차단
  ├── 동적 레벨 제한 (동시 DCA 과다 / SL 연타)
  ├── BTC Crash → 롱 DCA 차단 → 보험 트리거
  └── Killswitch MR≥0.8 → 전 DCA 차단 → 보험 트리거
```

### 4.4 plan_tp1 — Rebound Alpha TP1

```
tp1_thresh = min(worst_roi + alpha, alpha) × skew_mult × slot_mult / mr_pressure

V10.26 Alpha:
  T1: 4.0%  |  T2: 3.5%  |  T3: 2.5%  |  T4: 2.0% (floor 없음 → 소폭 손실 탈출)

Floor: T1~T3 → min 0.3% (항상 수익)  |  T4 → 없음

체결: 40% 부분 익절 → step=1 (trailing 전환) → 잔량 60% trailing
선주문: target price에 limit 선배치, worst_roi 변동 0.3%+ 시 reprice
```

### 4.5 plan_trail_on — Trailing Stop

```
조건: step ≥ 1 (TP1 완료 후)

FIXED Trail Gap: max_roi - 0.3% → stop
  max_roi=2% → stop=1.7%  |  max_roi=5% → stop=4.7%

TP1 Floor: CORE 0.1% / HEDGE 0.2% (최소 마지노선)

타임컷: CORE 45~120분 (ATR 보정) / SOFT_HEDGE 5~15분
```

### 4.6 plan_force_close — HARD_SL

```
V10.26 Tier별 SL:
  T1: -7.0%   (다음 DCA -5.0% + 2% buffer)
  T2: -8.5%   (다음 DCA -6.5% + 2% buffer)
  T3: -10.0%  (다음 DCA -8.0% + 2% buffer)
  T4: -10.0%  (DCA 소진 → MR 압력 적용 가능)

MR 압력 (T4만):
  MR 0.7 → SL -7.1%  |  MR 0.9 → SL -5.6%

기준가: tier별 entry price (t2_entry_price 등), 없으면 평균 ep
```

---

## 5. 스큐 관리 시스템

### 5.1 calc_skew

```python
skew = |long_margin - short_margin| / total_cap
# CORE_HEDGE, INSURANCE_SH 제외 (순수 MR 포지션만)
```

### 5.2 _skew_tp_adjustment (V10.26 연속 함수)

```
Heavy side: skew_mult = max(0.2, 1.0 - skew × 4.0)
Light side: skew_mult = min(3.0, 1.0 + skew × 7.0)

  skew  │  Heavy mult  │  Light mult  │  Full Close  │  Light Blocked
  <3%   │    1.0       │    1.0       │     ✗        │     ✗
   5%   │    0.80      │    1.35      │     ✗        │     ✗
  10%   │    0.60      │    1.70      │     ✅       │     ✗
  15%   │    0.40      │    차단       │     ✅       │     ✅
  15%+30분│   0.40      │    2.5       │     ✅       │     ✗ (탈출구)
  20%   │    0.20      │    차단       │     ✅       │     ✅

MR 압력 (MR≥0.5): skew_mult /= (1.0 + (mr-0.5) × 2.0)
  → MR 0.7: threshold 29% 하향  |  MR 0.9: 44% 하향
```

### 5.3 Escalation 체인

```
 스큐 5%   → Heavy TP1 가속 (skew_mult 0.80)
 스큐 10%  → Heavy 풀클로즈 + Light DCA T3 상한
 스큐 12%  → HEDGE_CORE 진입 고려 (stage2 타이머 시작)
 스큐 15%  → Light TP1 차단 + Light DCA T2 상한 + E30 활성화
             15분 지속 → 페어 컷 대기
             30분 지속 → Light TP1 탈출구 (2.5x)
 스큐 20%+ → Light DCA 완전 차단
```

### 5.4 plan_pair_cut (V10.26)

```
조건: 스큐 ≥12% + stage2 타이머 15분(15%+) 또는 20분(12~15%)
매칭: heavy ROI 최저 + light ROI 최고 → 동시 FORCE_CLOSE
안전장치: net_pnl > heavy 단독 HARD_SL 손실
쿨다운: 5분
```

---

## 6. 리스크 게이트 (risk_manager.py)

```
evaluate_intent 7단계:

  R0  스냅샷 유효성 ─── invalid → 전면 거부
  R1  DD 셧다운 ─────── 일간 DD ≤ -7% → 전면 거부
  R2  Kill Switch
       MR ≥ 0.9 → EXIT만 허용 (SYSTEM_FREEZE)
       MR ≥ 0.8 → OPEN/DCA 거부 (ASYM 면제)
       MR ≥ 0.7 → OPEN 거부
  R3  Toggle ─────────── use_long/use_short 수동 차단
  R4  슬롯 ──────────── 전체 10, MR 방향당 4, HEDGE 1
  R5  T4 최대 손실 ──── dca_level≥4 + ROI ≤ -7% → DCA 거부
  R6  쿨다운/상관성 ──── cooldown + corr ≥ 0.5(DCA) / 0.6(OPEN)
  R7  노출 캡 ────────── 방향 1.8x / 양방향 2.6x equity
```

---

## 7. 주문 실행 흐름

```
Intent 생성 (planners)
    ↓
evaluate_intent (risk_manager) → approved / rejected
    ↓
execute_intents (execution_engine)
  - 우선순위 정렬 (FORCE_CLOSE > TP1 > TRAIL > DCA > OPEN)
  - 같은 심볼 중복 실행 방지 (sym:side 키)
  - OPEN TICK_LIMIT=1 (틱당 1건)
    ↓
route_order (order_router)
  - TP1 → limit (지정가, 슬리피지 0)
  - TRAIL/FORCE_CLOSE → market (시장가, 즉시 체결)
  - OPEN/DCA → limit (5분 TTL, pending 추적)
  - INSURANCE_SH/CORE_HEDGE → market
  - DEDUP 300초 TTL (동일 주문 방지)
    ↓
_manage_pending_limits (runner, 매 틱)
  - limit 주문 상태 폴링 (open/closed/canceled)
  - 체결 → _apply_pending_fill → 포지션북 반영
  - 5분 미체결 → 취소
  - 텔레그램 알림 (TP1_LIMIT / PENDING_DCA / PENDING_OPEN)
    ↓
_manage_tp1_preorders (runner, 매 틱)
  - CORE 포지션별 target price 계산
  - 선주문 없으면 배치, 0.3%+ 변동 시 reprice
  - DCA/trailing/skew-blocked → 자동 취소
```

---

## 8. 포지션 라이프사이클

```
 OPEN (T1 15%)
   │
   ├──→ 상승 ──→ TP1 선주문 체결 (40%) ──→ Trailing (60%) ──→ Exit
   │                                           │
   │                                    max_roi - 0.3% stop
   │                                    또는 타임컷 45~120분
   │
   ├──→ 하락 -5% ──→ T2 DCA (20%, 쿨다운 10분)
   │      │
   │      ├──→ 반등 ──→ TP1 (alpha 3.5%) ──→ Trailing ──→ Exit
   │      │
   │      ├──→ 추가 하락 -6.5% ──→ T3 DCA (30%, 쿨다운 5분)
   │      │      │
   │      │      ├──→ 반등 ──→ TP1 (alpha 2.5%) ──→ Exit
   │      │      │
   │      │      └──→ 추가 하락 -8.0% ──→ T4 DCA (35%, 쿨다운 2분)
   │      │             │
   │      │             ├──→ 반등 ──→ TP1 (alpha 2.0%, 소폭 손실 허용)
   │      │             │
   │      │             └──→ -10.0% ──→ HARD_SL (MR 높으면 더 타이트)
   │      │
   │      └──→ DCA 미발동 (쿨다운/차단) ──→ -8.5% HARD_SL
   │
   └──→ DCA 없이 -7.0% ──→ HARD_SL (빠른 컷)
```

---

## 9. 헷지 시스템

### 9.1 CORE_HEDGE

```
진입 조건 (5단 AND):
  ① skew ≥ 12%
  ② skew ≥ 15% (stage2)
  ③ _is_hedge_required (OR 3가지):
       heavy side 전 슬롯 ROI < 0
       MR ≥ 0.7
       stage2 15분 지속
  ④ 소스: heavy side T1/T2(stressed) 또는 T2(normal), ROI ≤ -3%/-6%
  ⑤ MAX_HEDGE_SLOTS = 1

관리:
  소스 TP1/소멸 → MR 전환 (수익이면 trailing, 손실이면 독립 DCA)
  듀얼 프로핏: 양쪽 ROI ≥ 0.3% → 양쪽 40% TP1

알려진 이슈: MR 0.7+ 킬스위치가 CORE_HEDGE 진입 차단 (BUG-A 미수정)
```

### 9.2 INSURANCE_SH

```
트리거 (V10.26, 3가지만):
  BTC_CRASH     — BTC 급락 시 롱 DCA 차단
  KILLSWITCH    — MR ≥ 0.8 전 DCA 차단
  DCA_LIMIT     — 동시 DCA 과다 / SL 연타

100% 반대방향 진입 → 60초 타임컷 → 수익이면 trailing, 손실이면 청산
DCA 레벨당 1회 제한
```

---

## 10. 핵심 파라미터 (V10.26)

```
# 슬롯
TOTAL_MAX_SLOTS = 10   MAX_LONG = 5   MAX_SHORT = 5   MAX_MR_PER_SIDE = 4
MAX_HEDGE_SLOTS = 1    GRID_DIVISOR = 8

# 레버리지/수수료
LEVERAGE = 3   FEE_RATE = 0.0002

# DCA
DCA_WEIGHTS = [15, 20, 30, 35]
DCA_ROI_TRIGGERS = {2: -5.0, 3: -6.5, 4: -8.0}
DCA_COOLDOWN_BY_TIER = {2: 600, 3: 300, 4: 120}

# TP1
REBOUND_ALPHA = {1: 4.0, 2: 3.5, 3: 2.5, 4: 2.0}
TP1_PARTIAL_RATIO = 0.40   TP2_PCT = 4.0

# HARD_SL (planners 내 _TIER_SL)
T1: -7.0%   T2: -8.5%   T3: -10.0%   T4: -10.0% (+MR 압력)

# 스큐
SKEW_HEDGE_TRIGGER = 0.12   SKEW_STAGE2_TRIGGER = 0.15
SKEW_STAGE2_TIMEOUT_SEC = 900

# Kill Switch
MR ≥ 0.7 → OPEN 차단   MR ≥ 0.8 → DCA 차단   MR ≥ 0.9 → EXIT만
DD_SHUTDOWN_THRESHOLD = -7%
```

---

## 11. 데이터 흐름

```
Binance API
    │
    ├── OHLCV (1m/5m/15m/1h) ──→ ohlcv_pool
    ├── Ticker (현재가) ────────→ all_prices
    ├── Balance ──────────────→ real_balance_usdt
    ├── Positions ────────────→ _sync_positions_with_exchange
    └── Margin Ratio ─────────→ margin_ratio
         │
         ↓
    MarketSnapshot
         │
    ┌────┴──────────────────────────────────────────────┐
    │  planners: RSI, EMA, ATR, regime, skew 계산        │
    │  → Intent[] 생성                                    │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────┴──────────────────────────────────────────────┐
    │  risk_manager: R0~R7 게이트 → approved/rejected    │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────┴──────────────────────────────────────────────┐
    │  execution_engine → order_router → Binance API     │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────┴──────────────────────────────────────────────┐
    │  strategy_core: OrderResult → 포지션북 갱신         │
    │  position_book: JSON 저장/로드                      │
    │  logger_csv: 7개 CSV 로그                           │
    │  telegram_engine: 체결 알림                          │
    └───────────────────────────────────────────────────┘
```

---

## 12. 상태 저장

```
v9_state.json
  ├── st: {심볼: {p_long: {...}, p_short: {...}, pending_entry_*}}
  ├── cooldowns: {심볼: 만료시각}
  └── system_state:
        ├── initial_balance, baseline_balance, baseline_date
        ├── shutdown_active, shutdown_until
        ├── use_long, use_short
        ├── close_all_requested
        ├── _hard_sl_history, _insurance_last_dca
        ├── pending_asym_force, open_symbol_cd_until
        └── _boot_ts, _current_regime

포지션 필드 (p_long / p_short):
  ep, original_ep, amt, side, time, last_dca_time
  step (0=보유, 1=trailing), dca_level (1~4)
  worst_roi, max_roi_seen, dca_targets[]
  role (CORE_MR / CORE_HEDGE / INSURANCE_SH)
  entry_type (MR / E30 / MR_E30 / HEDGE_CORE)
  tp1_done, tp2_done, trailing_on_time
  tp1_preorder_id, tp1_preorder_price, tp1_preorder_ts
  pending_dca, insurance_sh_trigger
  source_sym, source_side (헷지 전용)
  locked_regime, max_dca_reached
  t2_entry_price, t3_entry_price, t4_entry_price
```

---

## 13. 로그 파일

| 파일 | 내용 | 주기 |
|---|---|---|
| log_intents.csv | 생성된 모든 Intent | 매 틱 |
| log_risk.csv | evaluate_intent 결과 (approve/reject) | 매 틱 |
| log_orders.csv | 거래소 주문 (placed/filled/DEDUP) | 주문 시 |
| log_fills.csv | 체결 확인 | 체결 시 |
| log_trades.csv | 포지션 종료 (PnL, ROI, hold_sec) | 청산 시 |
| log_positions.csv | 포지션 스냅샷 | 매 틱 |
| log_skew.csv | 스큐 모니터링 | 주기적 |
| log_ml_features.csv | ML 피처 (DCA 발동 시점 시장 상태) | DCA 시 |

---

## 14. 외부 연동

```
Binance USDT-M Futures API (ccxt)
  - Hedge Mode (LONG/SHORT positionSide)
  - set_leverage(3)
  - create_order (limit / market)
  - fetch_order (pending limit 추적)
  - cancel_order (타임아웃 / reprice)
  - fetch_positions (sync)
  - fetch_balance

Telegram Bot API
  - 체결 알림 (notify_fill / notify_async_fill)
  - 상태 리포트 (report_system_status)
  - 명령: /close_all, /status, /long_off, /short_off

Anthropic API (ai_engine.py)
  - 뉴스 감성 분석 (현재 보조 역할)
  - DCA 승인 판단 (현재 미사용)
```
