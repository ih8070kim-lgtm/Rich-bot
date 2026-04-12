# TREND — 추세 추종 진입

## 전략 개요
MR 시그널이 감지되면 반대 방향 추세가 강한 다른 심볼에 진입.
MR 슬롯 여유 시 TREND_COMP, 슬롯 풀 시 TREND_NOSLOT.

## 두 가지 경로

### TREND_COMP (MR 슬롯 여유 시)
```
MR 시그널 감지 (trigger_side 존재)
→ 반대 방향 심볼 중 score 최고 후보 선택
→ _pending_trend_comp에 저장
→ MR fill 확인 후 발사
→ role=CORE_MR, entry_type=TREND
```

### TREND_NOSLOT (MR 슬롯 풀 시) ★
```
MR 시그널 감지 BUT trigger_side=None (슬롯 블록)
→ 반대 방향 심볼 전체 스캔
→ 글로벌 최고 score 1개만 _noslot_best에 저장
→ 메인 루프 종료 후 즉시 발사 (intents.append)
→ _open_dir_cd 10분 쿨다운 적용
```

### ★ 절대 규칙: NOSLOT은 pending 금지
pending 구조는 MR fill 이벤트를 기다리는데, MR 슬롯 풀이면 fill이 없음.
반드시 intents.append로 즉시 발사해야 함.

### 버그 사례
- **2026-04-12**: NOSLOT이 _pending_trend_comp에 저장 → MR fill 영원히 안 옴 → 미발사
- **2026-04-12**: 쿨다운 없이 매 틱 발사 → 3개 동시 진입

## Score 계산 (_calc_trend_score)
```python
score = EMA이격(ATR단위) × 거래량서지(5봉/30봉) × (1 + |RSI극단|)
```
| score | 의미 |
|-------|------|
| 0.5~3 | 약한 추세 |
| 3~7 | 중간 |
| 7+ | 강한 추세 |

자격 기준: |score| > 0.5 (_TR_MIN=0.5)
선택 기준: 상대평가 1위 (score 최고만 발사)

## TREND vs 동일심볼 헷지 시뮬
```
[HEDGE_SIM]      → TREND 발동 시 동일심볼 반대 EP 기록
[HEDGE_SIM_EXIT] → MR 청산 시 시뮬 PnL vs 실제 PnL 비교
```
system_state["_hedge_sim"]에 저장. 2~3주 데이터로 비교 분석 가능.

## 진입 후 관리
TREND 포지션은 role=CORE_MR이므로 일반 MR과 동일하게 관리:
- DCA (T2/T3)
- Trim (T3→T2→T1)
- TP1 → Trail → Close
- HARD_SL / ZOMBIE

## 수정 시 체크
- [ ] NOSLOT이 intents.append 사용하는지 (pending 아님)
- [ ] _noslot_best가 글로벌 최고 score인지 (루프 내 즉시 발사 아님)
- [ ] _open_dir_cd 쿨다운 발사 전후 체크하는지
- [ ] TREND 포지션이 슬롯 카운팅에 포함되는지 (CORE_MR이므로 포함 맞음)
