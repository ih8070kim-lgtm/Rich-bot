# TRIM / TP1 — 체크리스트

## 함정
- ★ V10.30: TRIM은 trail 기반 — 고정값 즉시 발동 제거
- ★ V10.30: trim 선주문(LIMIT) 제거 — trim trail(시장가)로 대체
- ★ V10.30: T2+는 정규 TP1 경로 차단 (`continue`) — trim trail이 유일한 exit
- tp1_limit_oid / tp1_preorder_id가 DCA 시 클리어 안 되면 trim 영구 차단

## TRIM Trail 흐름 (V10.30)
```
T2 포지션:
  ROI ≥ 1.5% → trim_trail_active = True, max 추적 시작
  ROI 상승 → max 갱신
  ROI ≤ max - gap → TRIM 발동 (T2→T1)
  ROI ≤ 0.5% (floor) → 안전 TRIM 발동

T3 포지션:
  ROI ≥ 1.0% → trim_trail_active = True
  같은 trail 로직 → TRIM 발동 (T3→T2)

gap = ATR 15m 구간별:
  ATR < 0.30% → 0.2%
  ATR < 0.75% → 0.3%
  ATR ≥ 0.75% → 0.5%
```

## 수정 시 체크
- [ ] T2+가 정규 TP1로 빠지지 않는지 (dca_level >= 2 → continue)
- [ ] DCA 시 trim_trail_active / trim_trail_max 리셋
- [ ] DCA 시 tp1_limit_oid + tp1_preorder_id 클리어 + 취소큐
- [ ] trim 수량이 노셔널 기반(calc_trim_qty)인지
- [ ] trim 후 dca_level 정확히 감소
- [ ] TRIM_TRAIL_FLOOR(0.5%) 하한 유지
