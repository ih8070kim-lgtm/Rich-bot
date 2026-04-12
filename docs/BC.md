# BC — Beta Cycle (BTC 대비 과열 숏)

## 전략 개요
BTC 대비 excess return이 과열된 알트코인을 숏. x1 레버리지 독립전략.
슬롯 비점유, CORE_MR 로직(DCA/trim/trail)과 완전 분리.

## 진입 플로우
```
1. ARMED: excess return ≥ 5% (BC_ARM_THRESH)
   + 볼륨 필터: 최근 6h 거래량 ≥ 7일 평균 × 1.5 (스파이크 진위 확인)
2. 대기: excess가 자연 감소
3. ENTRY: excess ≤ 2% (BC_NORM_THRESH) + pullback 0.5~8%
```

## Beta 계산 (★ 상방 베타만)
```python
# BTC 상승 봉만 사용 → 하락 반등 숏 방지
_up_mask = btc_lr > 0
beta = cov(alt_up, btc_up) / var(btc_up)
```
1h: 7일 윈도우, 상승봉 최소 10개
1d: 30일 윈도우, 상승봉 최소 5개

### 상방 베타를 쓰는 이유
- 전체 베타: BTC 폭락 시 같이 빠진 코인도 beta 높게 나옴
- 상방 베타: BTC 랠리에서 더 많이 오른 코인만 높게 나옴
- 결과: 하락 후 반등(WLD 1d=-14.8%)은 자연스럽게 필터링

## Excess Return
```
excess = alt_return - (beta × btc_return)
```
1h: 24시간 윈도우 (BC_RETURN_WINDOW=24)
1d: 7일 윈도우 (RET_W=7)
OR 조건: 1h 또는 1d 중 하나라도 충족 시 발동

## ARMED 만료 (★ 타임프레임 분리)
```
1h 시그널 → 48시간 후 만료
1d 시그널 → 7일 후 만료
1h → 1d 승격 시 만료 리셋 (tf="1d", ts=갱신)
```

## 청산 조건
```
BC_SL:      price ≥ ep × (1 + 8%)   → 손절
BC_TP:      price ≤ ep × (1 - 6%)   → 익절
BC_TRAIL:   trail_active 후 trail_stop 돌파 → 트레일 청산
BC_TIMEOUT: 보유 336시간(14일) 초과  → 시간 만료
BC_REHEAT:  excess 다시 ≥ 5%        → thesis 실패 손절
```

## 제한
```
BC_MAX_POS = 2         하루 동시 보유 최대
BC_ENTRY_PER_DAY = 3   하루 진입 최대
BC_COOLDOWN_HOURS = 72 심볼별 3일 쿨다운
BC_SIZE_DIVISOR = 10   equity/10 ≈ 10%
```

## 슬롯 규칙
BC는 **모든** MR 슬롯 카운팅에서 제외:
- slot_manager.py count_slots
- planners.py _core_long/_core_short
- planners.py _HEDGE_ROLES_SLOT
- planners.py can_long/can_short

## 수정 시 체크
- [ ] 상방 베타 계산이 BTC 상승봉만 사용하는지
- [ ] 볼륨 필터가 ARMED 시 적용되는지
- [ ] ARMED 만료가 1h/1d 분리되는지
- [ ] _HEDGE_ROLES_SLOT에 "BC" 포함되는지
- [ ] order_router.py에서 lev=1 적용되는지
- [ ] runner.py 레버리지 초기화 후 BC 심볼 x1 복원하는지
