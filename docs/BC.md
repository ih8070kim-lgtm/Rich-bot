# BC — 체크리스트

## 함정
- 상방 베타만 사용 (BTC 상승봉). 전체 베타 쓰면 하락 반등도 잡아서 손실
- BC/CB는 모든 슬롯 카운팅에서 제외 — SLOTS.md 5곳 확인
- order_router에서 lev=1 적용. runner.py 레버리지 초기화 후 BC 심볼 x1 복원 필요
- MR HARD_SL이 BC 포지션 kill하는 버그 있었음 — BC role 체크로 분리됨
- ★ V10.30: 고정 NORM_THRESH 제거 → baseline 적응형 진입
- ★ V10.30: REHEAT 즉사 방지 — 진입 시 excess가 baseline으로 복귀해야 함

## 진입 흐름 (V10.30)
```
ARM: excess ≥ 5% (BC_ARM_THRESH)
  → baseline = ARM 직전 72h excess 중앙값 기록
  → peak_excess, peak_price 기록

ENTRY: 1h excess ≤ baseline ("진짜 식었다")
  + pullback 0.5~8%
  + 볼륨 확인

REHEAT: 1h excess ≥ 5% → 손절 (thesis 실패)
  → baseline 복귀 후 진입이므로 즉사 구조적 불가
```

## 수정 시 체크
- [ ] _HEDGE_ROLES_SLOT에 "BC" 포함
- [ ] order_router lev=1
- [ ] runner 레버리지 복원 시 _bc_cb_role_map 참조
- [ ] HARD_SL/ZOMBIE에서 BC role 제외
- [ ] 상방 베타 계산 (_up_mask) 유지
- [ ] _calc_baseline_excess가 72h 구간 계산하는지
- [ ] ARM 시 baseline이 _armed dict에 저장되는지
- [ ] 진입 시 bc_baseline이 position metadata에 저장되는지

## ★ V10.31c 변경
- **TRAIL에서 wick(h_1h) 제거** — `price >= trail_stop`만 체크. SL은 wick 유지(손실 방어).
  - 변경 전: 1시간봉 고가가 trail_stop 넘으면 즉시 청산 → 순간 스파이크로 과민반응
  - 변경 후: 현재가가 trail_stop 넘을 때만 청산 → 노이즈 감소
- **peak_roi / giveback shadow logging 추가** (`BC_EXIT` 이벤트)
  - `p["bc_peak_roi"]`: 매 틱 갱신되는 최고 ROI
  - 청산 시: `exit=±x% peak=+y% giveback=+z% hold=nh`
  - 2~3주 데이터 수집 후 activation 3%/floor 1.5% 재검토
