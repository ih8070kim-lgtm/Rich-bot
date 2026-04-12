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
