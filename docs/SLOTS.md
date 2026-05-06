# SLOTS — 슬롯 관리 규칙

## 슬롯 구조
```
총 슬롯: TOTAL_MAX_SLOTS = 10 (양방향 합산)
방향별:  MAX_LONG = 5, MAX_SHORT = 5
MR 한도: MAX_MR_PER_SIDE = 4 (방향당 MR 최대)
```

## 슬롯 카운팅 제외 대상
아래 role은 **모든** 슬롯 카운팅에서 제외:
```
HEDGE, SOFT_HEDGE, INSURANCE_SH, CORE_HEDGE, BC, CB
```

### 제외해야 하는 위치 (전부 확인 필수)
| 파일 | 위치 | 변수/로직 |
|------|------|-----------|
| slot_manager.py | count_slots() | `if role in ("BC","CB"): continue` |
| planners.py | _core_long/_core_short 카운팅 | `if role in ("HEDGE","SOFT_HEDGE","INSURANCE_SH","CORE_HEDGE","BC","CB"): continue` |
| planners.py | _HEDGE_ROLES_SLOT 세트 | BC, CB 포함 확인 |
| planners.py | _HEDGE_ROLES_U 세트 | BC, CB 포함 확인 |
| planners.py | can_long/can_short | _has_core_long/short에서 BC/CB 포지션 걸리면 안 됨 |

### 버그 사례
- **2026-04-12**: planners.py `_core_short` 카운팅에서 BC 미제외
  → BC 숏 1개 + TREND 숏 3개 = 4 → MAX_MR_PER_SIDE 도달 → TREND 4번째 차단
  → slot_manager.py는 수정했는데 planners.py 놓침

## 동적 슬롯 확장
```
DYNAMIC_SLOT_INITIAL = 2           → 시작 슬롯
한쪽 T2 발생 → 반대 방향 +1 (3)
한쪽 T3 발생 또는 T2 2개 → 반대 방향 +2 (4)
```

## Rule A: Slot Balance Gate
```
반대방향 = 0 AND 이쪽 ≥ 3 → 신규 진입 차단
목적: 한쪽만 가득 차는 것 방지
```

## Killswitch (Margin Ratio 기반)
```
MR ≥ 0.80 → OPEN 금지 (신규만)
MR ≥ 0.85 → OPEN + DCA 금지
MR ≥ 0.90 → 전체 동결 (청산만 허용)
```


---

## V14.1 [05-06] — 슬롯 분리 (CORE_MR vs CORE_MR_HEDGE)

### 변경
V10.31u 규칙 (CORE_MR_HEDGE를 CORE_MR 슬롯에 합쳐 카운트) 제거.

### 슬롯 카운트 로직 (V14.1)
```
count_slots(role_filter="CORE_MR"):
  - CORE_MR + CORE_BREAKOUT만 카운트
  - CORE_MR_HEDGE 제외 (V14.0까지는 포함했음)
  - BC/CB/HEDGE/SOFT_HEDGE/INSURANCE_SH 제외 (이전과 동일)

CORE_MR_HEDGE는 별도 직접 카운트 (planners.py:1318):
  - planners.py가 활성 포지션 순회하며 카운트
  - HEDGE 슬롯 한도 = MAX_MR_PER_SIDE (4)
```

### 1대1 매칭 작동
1. plan_open MR 진입 시그널 감지 → CORE_MR 슬롯 체크 (4 한도)
2. MR intent 발사 → TREND_COMP 트리거 → HEDGE 슬롯 체크 (4 한도)
3. MR fill 후 TREND_COMP 발사
4. 결과: MR 4쌍 + TREND_COMP 4쌍 = 8 포지션 동시 가능

### 위험
- 자본 노출 2배 (마진 사용량 증가)
- 변동성 시기 양쪽 SL 발생 시 손실 임팩트 2배

### 매트릭스
| 시나리오 | V14.0 (합쳐 카운트) | V14.1 (분리) |
|---|---|---|
| MR LINK BUY + AVAX TREND SHORT | CORE_MR (1L, 1S) | CORE_MR (1L, 0S) + HEDGE (0L, 1S) |
| MR 4쌍 모두 활성 | 슬롯 풀 가능성 | MR 4 + HEDGE 4 별도 |
| 다음 MR 진입 시도 | 슬롯풀 차단 가능 | MR 슬롯만 체크 |

### V11 호환
DCA_WEIGHTS=[100] 시 TREND_COMP 진입 안 함 → V14.1 변경 영향 0. V11 단순 운영 그대로.
