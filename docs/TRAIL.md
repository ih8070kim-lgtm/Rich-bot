# TRAIL — 트레일링 스탑 & 청산

## Trail 진입 조건
```
step=1 (TP1 체결 후) AND max_roi ≥ TP1 기준
```

## Fixed Trail 로직
```
FIXED_TRAIL_GAP = 0.3%  (V10.29e)
stop = max_roi - 0.3
ROI가 stop 이하로 하락 → trail close 발동
```
예: max_roi=2.5% → stop=2.2% → ROI 2.2% 이하 시 청산

## Trail Close 수량
```
trail_qty = p["amt"] 전량 (T1 잔량 전부)
```

### ★ 절대 규칙: trail close는 전량
노셔널 기반 부분 수량(calc_tier_notional)을 사용하면 잔량이 남음.
잔량은 봇이 관리 못 하는 유령 포지션이 됨.

### 버그 사례
- **2026-04-12**: OP/USDT qty=2939.7인데 571.7만 trail close
  → tp1_preorder가 1184를 limit으로 미리 걸어서 체결됨
  → p["amt"]이 이미 줄어든 상태에서 trail이 나머지 닫음 (정상)
  → 하지만 stale limit이 있으면 잔량 발생 가능

## Trail Timeout
```
TRAILING_TIMEOUT_MIN = 45분
step=1 진입 후 45분 경과 → 강제 청산 (시간 기반)
```

## 청산 유형 (hedge_engine.py plan_force_close)
```
HARD_SL:      ROI ≤ HARD_SL_BY_TIER → 손절
              T1: -3.8%, T2: -5.6%, T3: -10.0%

ZOMBIE:       T1/T2 보유 12시간 + ROI ≤ -5% → 시간 기반 손절
              쿨다운 8시간 (같은 방향)

T3_DEF:       T3 worst_roi ≤ -7% → 방어 청산
              tp = worst + gap, gap = worst/(-7%) 비례

DD_SHUTDOWN:  계좌 DD ≤ -7% → 전 포지션 청산 + 12시간 동결
```

## 수정 시 체크
- [ ] trail_qty가 p["amt"] 전량인지 (부분 수량 사용 금지)
- [ ] step=1 조건이 유지되는지
- [ ] FIXED_TRAIL_GAP 값이 config나 planners에서 일관되는지
- [ ] plan_force_close의 HARD_SL_BY_TIER가 config와 일치하는지
