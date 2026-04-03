# Trinity V10.13 Architecture Document
## For LLM Migration / Context Transfer
### Last Updated: 2026-03-27 최종 아키텍처 확정

## 1. 시스템 철학

**메인:** 소스 단독 복구형 MR
**보조:** 헷지는 상시 알파가 아니라 제한형 tail-risk 보험

- 평소엔 소스만 돈다
- 극단 조건에서만 hedge 1개 허용
- 기본값은 hedge 최소화

## 2. Directory Structure
```
Rich-bot/
├── main.py, trinity_supervisor.py, watchdog.py
├── telegram_bot.py (관제), telegram_deploy_bot.py (배포)
└── v9/
    ├── config.py, types.py
    ├── app/runner.py
    ├── datafeed/market_snapshot.py, universe_asym_v2.py
    ├── engines/hedge_core.py ★, hedge_engine_v2.py, dca_engine.py
    ├── execution/execution_engine.py, order_router.py ★, position_book.py
    ├── strategy/planners.py ★, strategy_core.py ★
    ├── risk/risk_manager.py, slot_manager.py, exposure.py
    ├── logging/logger_csv.py, logger_ml.py, schemas.py
    └── utils/utils_math.py, utils_time.py
```

## 3. Main Loop (runner.py, ~10초/틱)
Intent 생성 순서:
plan_force_close → plan_hedge_core_manage → plan_tp1 → plan_trail_on
→ plan_dca → plan_insurance_sh → plan_open

## 4. 소스 코어 규칙

### 4.1 포지션 관리 기본값 (bt_source 우승안 W5.0)
- width = 5.0, tp1_mode = B, dca_weights = [20,20,20,20,20]
- dca_dist_mult = 1.1

### 4.2 DCA 규칙
- 평균단가(ep) 기준 -8.25% ROI 도달 시 체결 (-1.5 × 5.0 × 1.1)
- T2~T5 전 티어 동일 간격
- DCA 체결 후 worst_roi 새 ep 기준으로 리셋 (TP1 조기 발동 방지)
- 쿨다운: T2/3=1800s, T4=2700s, T5=3600s
- plan_dca에서 저장된 roi_trigger 무시 → 항상 config 기준 (런타임 즉시 반영)

### 4.3 TP1 규칙 — Min ROI 반등
- T1~T2: worst_roi + 10.0% (10.0 = 2.0 × W5.0 × TM1.0)
- T3~T5: worst_roi + 7.5%  (7.5  = 1.5 × W5.0 × TM1.0)
- 40% 부분청산 + 60% trailing
- limit 선주문 (maker), DCA 체결 시 취소→재주문

### 4.4 SL 규칙 — 완전 구간분리
- T1~T4: tier entry 기준 -11.2% ROI (다음 DCA 있으므로 공통)
- T5: -10.0% ROI (다음 DCA 없음 → 별도 빠른 종료)

### 4.5 Trailing
- squeeze=0.3, base = 1.50% (0.30 × W5.0)
- Progressive: dist = 1.50 / (1 + max(0,mr) × 0.6)
- Timecut: 120분

## 5. MR 진입
- Long:  curr_p < EMA10_15m - ATR×2.4(HIGH:3.0) AND RSI≤35/hook AND micro↑
- Short: (curr_p > EMA5_15m + ATR×1.8) OR (curr_p > EMA10_15m + ATR×2.4)
         AND RSI≥65/hook AND micro↓
- 상관성 하한: 0.50, FK Filter, BTC Crash Filter, 쿨다운 600s
- SKEW_MR 삭제 (최종 아키텍처)

## 6. CORE_HEDGE — tail-risk 보험
**기본안:**
- trigger_tier = T2만 (T1/T3+ 제외)
- source ROI ≤ -6.0% (진입 후 충분히 빠진 경우만)
- skew ≥ 12% (SKEW_HEDGE_TRIGGER)
- MAX_HEDGE_SLOTS = 1 (동시 1개만)

**소스 상태별 처리:**
| 소스 상태 | 헷지 처리 |
|-----------|-----------|
| 건재 + 양쪽 ROI ≥ 0.3% | DUAL_PROFIT: 동시 TP1 |
| T5 도달 | T5 미니게임 즉시 시작 |
| TP1/청산 + hedge ROI ≥ 0.4% | 즉시 FORCE_CLOSE |
| TP1/청산 + hedge ROI < 0.4% | CORE_MR 전환 |

**T5 미니게임:**
- DCA 없음, alpha=1.0, SL = start_price 기준 -1.5% ROI
- 소스 T5 체결 순간 즉시 시작

## 7. 보험 (INSURANCE_SH)
- DCA 조건 충족 + 차단 → 반대방향, 100% 사이즈
- 타임컷: BTC_CRASH/KILLSWITCH=300s, COOLDOWN=180s

## 8. 주문 라우팅
- OPEN/DCA: limit / CORE_HEDGE: 즉시 시장가 / INSURANCE_SH: 시장가
- TP1/TRAIL/FC: 시장가

## 9. 핵심 설정값
```
LEVERAGE=3, MAX_MR_PER_SIDE=4, MAX_HEDGE_SLOTS=1 (tail-risk 1개)
FEE=0.0002(maker)

DCA_ROI = -8.25% (T2~T5 공통, ep 기준)
DCA_WEIGHTS = [20,20,20,20,20]
DCA_COOLDOWN: T2/3=1800s, T4=2700s, T5=3600s

REBOUND_ALPHA: T1/2=10.0, T3~5=7.5
TP1_RATIO=0.40

SL_T1~4 = -11.2% ROI (tier entry 기준)
SL_T5   = -10.0% ROI
T5_MINI_SL = -1.5% ROI (start_price 기준)

TRAIL_GAP=1.50%, TRAIL_SHRINK=0.6, TRAIL_TOUT=120분
LONG_ATR=2.4(HIGH:3.0), SHORT_ATR=1.8(HIGH:2.4) + OR EMA10+2.4 미러링
RSI_OS=35, RSI_OB=65, OPEN_CORR_MIN=0.50
SKEW_HEDGE=0.12 (T2 소스, ROI≤-6%)
LONG/SHORT_MIN_CORR=0.50
```

## 10. 세션 누적 패치 요약 (2026-03-27)
| 버그/변경 | 파일 | 내용 |
|-----------|------|------|
| BUG1 | hedge_core | source_sym 유실 역탐색 복구 |
| BUG2 | strategy_core | DCA 후 worst_roi 새 ep 리셋 |
| BUG3 | planners | tier_cooldown 루프 내 계산 |
| BUG4 | planners | T3+ 쿨다운도 보험 트리거 |
| BUG5 | planners | 부팅 가드 trigger 클리어 제거 |
| BUG6 | hedge_core | calc_skew 헷지 role 제외 |
| FEAT | order_router | CORE_HEDGE 시장가 |
| FEAT | hedge_core | 소스 청산+수익≥0.4% → 즉시 청산 |
| FEAT | strategy_core | T5 미니게임 즉시 시작 |
| FEAT | planners | 숏 MR OR 미러링 (EMA10+2.4×) |
| 최종 | config | DCA=-8.25% 통일, SKEW_MR 삭제 |
| 최종 | config | REBOUND_ALPHA T1/2=10 T3~=7.5 |
| 최종 | config | SL T1~4=-11.2% T5=-10.0% |
| 최종 | config | MAX_HEDGE_SLOTS=1 |
| 최종 | hedge_core | T2 소스만, ROI≤-6% 조건 |
| 최종 | planners | SKEW_MR 블록 완전 삭제 |

## 11. 인프라
- supervisor: main + telegram_bot + watchdog
- deploy bot: 텔레그램→배포+GitHub sync+재시작
- 관제봇: /status /perf /regime /unlock /log
