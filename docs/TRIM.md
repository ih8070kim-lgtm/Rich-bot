# TRIM / TP1 — 체크리스트

## 함정
- ★ V10.31b: 전 tier trail 통합 — T1/T2/T3 모두 동일한 trail 메커니즘
- ★ V10.31b: tp1_preorder_id / tp1_limit_oid 필드 완전 제거 — plan_tp1이 잔존 시 자동 취소+정리
- ★ V10.31b: _manage_tp1_preorders 비활성화 — 선주문 시스템 전면 제거
- plan_tp1은 T1 전용 (trail → partial close → step=1 → TRAIL_ON)
- plan_trim_trail은 T2+ 전용 (trail → trim → tier 감소)

## TP Trail 흐름 (V10.31b)
```
■ T1 (plan_tp1)
  guard: step=0, tp1_done=False, not pending_close, role not BC/CB/HEDGE
  (tp1_preorder_id, tp1_limit_oid, exit_fail_cooldown 체크 없음)

  ROI ≥ 2.0% → trim_trail_active = True, max 추적 시작
  ROI 상승 → max 갱신
  ROI ≤ max - gap → TP1 발동 (시장가, partial close)
  ROI ≤ 0.5% (floor) → 안전 TP1 발동
  → step=1, tp1_done=True → TRAIL_ON이 잔량 처리

■ T2 (plan_trim_trail)
  guard: amt > 0, dca_level ≥ 2, not BC/CB, not pending_close
  
  ROI ≥ 1.5% → trim_trail_active = True
  ROI ≤ max - gap → TRIM 발동 (T2→T1)
  ROI ≤ 0.5% (floor) → 안전 TRIM 발동

■ T3 (plan_trim_trail)
  ROI ≥ 1.0% → trim_trail_active = True
  같은 로직 → TRIM 발동 (T3→T2)

■ gap = ATR 15m 구간별:
  ATR < 0.30% → 0.2%
  ATR < 0.75% → 0.3%
  ATR ≥ 0.75% → 0.5%
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
