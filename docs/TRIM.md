# TRIM / TP1 — 부분 익절

## TP1 (1차 익절)
```
T1 TP1: ROI ≥ 2.0%  (TP1_FIXED)
T2 TP1: ROI ≥ 1.5%
T3 TP1: ROI ≥ 2.4%
```
TP1 체결 → step=1 → trailing 시작

## TP1 Preorder (limit)
```
ROI가 TP1 근처(TRIM_PREORDER_ROI=1.0%)에 도달하면 limit 주문 선배치.
체결되면 strategy_core에서 step=1 전환.
```

### TP1 limit 관련 주의사항
- tp1_limit_oid: 바이낸스에 걸린 TP1 limit 주문 ID
- tp1_preorder_id: preorder 관리용 ID
- DCA 발생 시 반드시 두 필드 모두 클리어 + 바이낸스 취소큐 추가
- 미클리어 시 "trim 영구 차단" 버그 발생

## DCA Trim (tier 복귀)
```
T3 → T2 trim: ROI ≥ 1.0%  (TRIM_BLENDED_ROI_BY_TIER)
T2 → T1 trim: ROI ≥ 1.5%
```
trim 체결 → dca_level 감소 → DCA 재활용 가능

### Trim 수량 계산 (노셔널 기반)
```python
target_notional = calc_tier_notional(target_tier, balance)
trim_qty = total_amt - notional_to_qty(target_notional, price)
```
T3→T2: 총량에서 T2 목표 수량 뺀 나머지 매도
T2→T1: 총량에서 T1 목표 수량 뺀 나머지 매도

## Trim 선주문 (Preorder)
```
T3: ROI ≥ (trim기준 - 0.5%) 도달 시 limit 선배치
T2: ROI ≥ (trim기준 - 0.5%) 도달 시 limit 선배치
```
선주문은 p["trim_preorders"]에 저장. 포지션 청산 시 취소큐에 추가.

## DCA-Trim 사이클
```
T1 진입 → 역행 → T2 DCA → 반등 → T2 trim → T1 복귀 → 역행 → T2 DCA → 반등 ...
이 사이클이 Trinity 수익의 핵심 엔진.
```

## 수정 시 체크
- [ ] TP1 limit 관련 필드가 DCA 시 클리어되는지
- [ ] trim 수량이 노셔널 기반으로 계산되는지
- [ ] trim 후 dca_level이 정확히 감소하는지
- [ ] trim_preorders가 포지션 청산 시 취소큐에 들어가는지
