# CB — Crash Bounce (BTC 폭락 반등 롱)

## 전략 개요
BTC 급락 후 고베타 알트코인 반등 롱. x1 레버리지 독립전략.
슬롯 비점유, CORE_MR 로직과 완전 분리.

## 크래시 감지
```
BTC 4h ROC ≤ -5%  (CB_CRASH_4H)    + 볼륨 서지 ≥ 1.0
BTC 24h ROC ≤ -8% (CB_CRASH_24H)   단독 트리거 (볼륨 불필요)
```

## 진입 대상 선택
```
1. BTC와 상관계수 높은 알트 중 베타 상위 3개 (CB_TOP_BETA_N)
2. 각 심볼에 동시 진입 (최대 CB_MAX_ENTRIES=3)
```

## 포지션 사이징
```
notional = equity × CB_SIZE_PCT(10%)
qty = notional / price
레버리지: x1 (독립)
```

## 청산 조건
```
SL:      ROI ≤ -3%     (CB_SL_PCT)
TRAIL:   ROI ≥ 2% 후 ATR×1.0 트레일 (최소 1%)
TIMEOUT: 48시간 초과    (CB_MAX_HOLD_H)
```

## 제한
```
CB_MAX_POS = 3       동시 최대
CB_MAX_ENTRIES = 3   크래시당 최대
CB_COOLDOWN_H = 48   크래시 이벤트 쿨다운
```

## 슬롯 규칙
CB도 BC와 동일하게 **모든** 슬롯 카운팅에서 제외.

## 수정 시 체크
- [ ] _HEDGE_ROLES_SLOT에 "CB" 포함
- [ ] order_router.py에서 lev=1
- [ ] runner.py 복구 시 _bc_cb_role_map에 CB role 저장/복원
- [ ] CORR_GUARD에서 CB 제외
- [ ] 텔레그램 배지 ⚡ 표시
