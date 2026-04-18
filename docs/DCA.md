# DCA — 체크리스트

## 함정
- DCA 체결 시 tp1_limit_oid / tp1_preorder_id 미클리어 → trim 영구 차단 (04-12 버그)
- ★ V10.30: plan_dca(시장가) 제거 — _place_dca_preorders(LIMIT)로 단일화
- ★ V10.30: DCA 주문 전 목표 노셔널 대비 부족분만 주문 (과주문 방지)
- ★ V10.31c: **plan_dca 함수 자체도 삭제됨** (V10.30 호출 제거 후 함수 정의만 잔존하던 죽은 코드 276줄)
- T4/T5 코드 잔존하나 DCA_WEIGHTS=[25,25,50] 3티어라 도달 불가 (죽은 코드, 무해)

## DCA 경로 (V10.30)
```
단일 경로: runner._place_dca_preorders → LIMIT 주문
  - activation ROI 도달 시만 LIMIT 배치
  - deactivation ROI 초과 시 LIMIT 취소 (반등)
  - 목표 노셔널 = calc_tier_notional(tier, bal)
  - 주문 qty = (목표 노셔널 - 현재 보유 노셔널) / price
  - 부족분 ≤ 0 → SKIP (과주문 방지)
```

## DCA 체결 시 필수 클리어 (runner._apply_pending_fill)
```
tp1_limit_oid → pop + 취소큐
tp1_preorder_id → None
tp1_preorder_price → None
tp1_done → False
step → 0
trailing_on_time → None
max_roi_seen → 0.0
worst_roi → 0.0
trim_trail_active → False
trim_trail_max → 0.0
```

## 수정 시 체크
- [ ] 위 필드 클리어가 runner DCA fill 핸들러에 유지되는지
- [ ] DCA 주문 전 calc_tier_notional - 현재보유 검증 (양쪽 경로)
- [ ] ep 계산이 블렌디드 방식 유지되는지
- [ ] **DCA 선주문(dca_preorders)이 DCA fill/trim fill 시 전부 취소되는지**
- [ ] **DCA 선주문이 타임아웃 면제(is_dca_pre)되는지**
- [ ] **plan_dca 호출이 제거되었는지 (generate_all_intents)**
