# TRIM / TP1 — 체크리스트

## 함정
- ★ V10.31b: 전 tier trail 통합 — T1/T2/T3 모두 동일한 trail 메커니즘
- ★ V10.31c: `_manage_tp1_preorders`는 **LOW/NORMAL 레짐에서 활성 유지** (runner.py:2628에서 호출 중). V10.31b의 "선주문 시스템 전면 제거" 기재는 틀렸음 — 실제로는 HIGH에서만 trail 사용, LOW/NORMAL은 TP1 선주문 유지
- ★ V10.31g: **T3 trim은 레짐 불문 LIMIT 선주문 경로**. plan_trim_trail의 trail 모드는 T2 전용으로 축소
- plan_tp1은 T1 전용 (trail → partial close → step=1 → TRAIL_ON)
- plan_trim_trail은 T2 전용 (★ V10.31g: T3 제외) — trail → trim → tier 감소

## TP Trail 흐름 (V10.31g)
```
■ 레짐 분기
  T1 HIGH → trail (시장가, 추세 수익 포착)
  T1 LOW/NORMAL → 선주문 limit (지정가, maker, 슬리피지 0)
  T2 HIGH → trail (시장가)
  T2 LOW/NORMAL → 선주문 limit
  T3 ALL regime → 선주문 limit (★ V10.31g: HIGH에서도 LIMIT 유지)

■ T1 HIGH (plan_tp1 trail)
  ROI ≥ 2.0% → trail 활성화 → max-gap 하회 시 시장가 partial close
  → step=1 → TRAIL_ON이 잔량 처리

■ T1 LOW/NORMAL (_manage_tp1_preorders)
  TP 가격 계산 → 거래소 limit 선주문
  가격 도달 → 거래소 자동 체결 → _manage_pending_limits 처리
  → step=1 → TRAIL_ON이 잔량 처리

■ T2 HIGH (plan_trim_trail trail)
  ROI ≥ threshold(+1.5%) → trail 활성화 → 시장가 TRIM

■ T2 LOW/NORMAL (_place_trim_preorders)
  DCA 체결 시 calc_trim_price → limit 선주문
  기존 T2 포지션도 자동 재생성 (regen)
  가격 도달 → 체결 → tier 감소 (T2→T1)

■ T3 ALL regime (★ V10.31g) (_place_trim_preorders)
  threshold +0.5% — HIGH trail로 가면 변동성에 0.2% 이익만 먹고 이탈 → 재T3 → 강제청산 패턴
  LIMIT으로 +0.5% 정확 도달 시 maker(0.02%)로 깔끔히 T2 감소

■ 전환 안전장치
  T2: LOW→HIGH: 선주문 취소 → trail 시작 / HIGH→LOW: trail 리셋 → 다음 틱 선주문 배치
  T3: 레짐 전환에 영향 받지 않음 — 항상 LIMIT 경로 (V10.31g)
       기존 HIGH+T3 trail 활성 잔존 시 plan_trim_trail 진입부에서 자동 정리
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
- [ ] plan_trim_trail이 T3을 제외하는지 (★ V10.31g: `dca_level >= 3` skip)
- [ ] _place_trim_preorders가 HIGH+T3 케이스를 fall through 시키는지 (★ V10.31g)
- [ ] DCA 시 trim_trail_active / trim_trail_max 리셋 (runner.py)
- [ ] trim 수량이 노셔널 기반(calc_trim_qty)인지
- [ ] trim 후 dca_level 정확히 감소
- [ ] T1 TP1 intent에 force_market: True 포함
- [ ] T2 TRIM intent에 force_market: True 포함 (T3은 LIMIT이므로 force_market 없음)
- [ ] GHOST_CLEANUP 시 거래소 DCA/trim limit 취소 (runner.py sync)
