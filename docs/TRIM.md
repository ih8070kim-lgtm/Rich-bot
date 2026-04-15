# TRIM / TP1 — 체크리스트

## 함정
- ★ V10.31b: 전 tier trail 통합 — T1/T2/T3 모두 동일한 trail 메커니즘
- ★ V10.31b: tp1_preorder_id / tp1_limit_oid 필드 완전 제거 — plan_tp1이 잔존 시 자동 취소+정리
- ★ V10.31b: _manage_tp1_preorders 비활성화 — 선주문 시스템 전면 제거
- plan_tp1은 T1 전용 (trail → partial close → step=1 → TRAIL_ON)
- plan_trim_trail은 T2+ 전용 (trail → trim → tier 감소)

## TP Trail 흐름 (V10.31b)
```
■ 레짐 분기
  HIGH → trail (시장가, 추세 수익 포착)
  LOW/NORMAL → 선주문 limit (지정가, maker 수수료, 슬리피지 0)

■ T1 HIGH (plan_tp1 trail)
  ROI ≥ 2.0% → trail 활성화 → max-gap 하회 시 시장가 partial close
  → step=1 → TRAIL_ON이 잔량 처리

■ T1 LOW/NORMAL (_manage_tp1_preorders)
  TP 가격 계산 → 거래소 limit 선주문
  가격 도달 → 거래소 자동 체결 → _manage_pending_limits 처리
  → step=1 → TRAIL_ON이 잔량 처리

■ T2+ HIGH (plan_trim_trail trail)
  ROI ≥ threshold → trail 활성화 → 시장가 TRIM

■ T2+ LOW/NORMAL (_place_trim_preorders)
  DCA 체결 시 calc_trim_price → limit 선주문
  기존 T2+ 포지션도 자동 재생성 (regen)
  가격 도달 → 체결 → tier 감소

■ 전환 안전장치
  LOW→HIGH: 선주문 취소 → trail 시작
  HIGH→LOW: trail 리셋 → 다음 틱 선주문 배치
  DCA: 둘 다 리셋 (runner DCA fill handler)
```

## 제거된 것 (V10.31b)
- _manage_tp1_preorders 함수 호출 (runner.py에서 주석처리)
- tp1_preorder_id / tp1_limit_oid blocking 가드
- tp1_limit_oid 세팅 (strategy_core.py에서 주석처리)
- exit_fail_cooldown_until이 trail tracking 차단하던 로직
- TP1 limit 주문 → 시장가(force_market) 전환

## 수정 시 체크
- [ ] plan_tp1에 tp1_preorder_id / tp1_limit_oid 가드 없는지
- [ ] plan_tp1의 T1이 trail 방식인지 (즉시 TP 아님)
- [ ] plan_trim_trail이 generate_all_intents에서 호출되는지
- [ ] DCA 시 trim_trail_active / trim_trail_max 리셋 (runner.py)
- [ ] trim 수량이 노셔널 기반(calc_trim_qty)인지
- [ ] trim 후 dca_level 정확히 감소
- [ ] TRIM_TRAIL_FLOOR(0.5%) 하한 유지
- [ ] T1 TP1 intent에 force_market: True 포함
- [ ] T2+ TRIM intent에 force_market: True 포함
- [ ] GHOST_CLEANUP 시 거래소 DCA/trim limit 취소 (runner.py sync)
