# V9.1 Trinity 패치 — 변경 사항 요약

## 사용자 결정 반영
- 헷지 트리거: **T4 진입 즉시** (roi 조건 제거)
- 반대방향 빈 슬롯: **유니버스 1순위 코인 즉시 오픈**
- DCA 비율: **1.0/0.8/0.8/2.0** (합산 4.6)

## 수정 파일 (10개)

| # | 파일 | 핵심 변경 |
|---|------|-----------|
| 1 | config.py | DCA 비율, MR 0.9, TP1 DCA별, Stage2, Sticky |
| 2 | slot_manager.py | SOFT 0.5 가중치 + 동적 슬롯 2→3→4 |
| 3 | hedge_engine.py | 무한 헷지 완전 재설계 (A/B/C/D) |
| 4 | planners.py | T4 즉시, 신규 오픈, DCA-level TP1, hedge TP1 제외 |
| 5 | dca_engine.py | hedge_mode DCA 허용 |
| 6 | risk_manager.py | MR 0.9 동결, CorrGuard 면제, corr 게이트 |
| 7 | strategy_core.py | Stage2 격상, hedge dca_targets |
| 8 | universe_asym_v2.py | Sticky 10분 안정화 |
| 9 | runner.py | 텔레그램 전체 청산, hedge_mode 전달 |
| 10 | telegram_bot.py | V9 통합, LEVERAGE 2.8, /close_all |

## 11개 이슈 수정 현황
1. ✅ 반대방향 빈 슬롯 → 신규 오픈 (planners.py)
2. ✅ 헷지 익절 +2.5% → 소스+헷지 동시 청산 (hedge_engine.py)
3. ✅ Stage 2 (2.4x) 격상 경로 (hedge_engine.py)
4. ✅ 무한 사이클 전이 (planners.py + hedge_engine.py)
5. ✅ 헷지 포지션 일반 DCA (dca_engine.py)
6. ✅ 동적 슬롯 개방 (slot_manager.py)
7. ✅ SOFT 0.5 가중치 (slot_manager.py)
8. ✅ LEVERAGE 통일 2.8 (telegram_bot.py)
9. ✅ 헷지 메타데이터 보존 (strategy_core.py hedge_stage)
10. ✅ hedge_mode TP1 제외 (planners.py)
11. ✅ CorrGuard 헷지 면제 (risk_manager.py)

## 검증 결과
- slot_manager: 7 tests passed
- hedge_engine: 6 tests passed
- planners (TP1): 7 tests passed
- risk_manager: 4 tests passed
- 통합 시뮬레이션: 7/7 passed
