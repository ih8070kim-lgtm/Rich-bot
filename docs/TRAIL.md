# TRAIL — 체크리스트

## 함정
- trail close는 반드시 p["amt"] 전량. 부분 수량 사용 시 유령 포지션 발생
- step < 1이면 trailing 진입 안 됨 — TP1 체결로 step=1 전환 필수
- ★ V10.31b: TP1도 trail 방식 — tp1_preorder/tp1_limit_oid 없음
- TP1 체결 시 max_roi_seen을 snapshot ROI로 덮어쓰면 안 됨 — 반드시 max(현재ROI, 기존max) 사용

## Trail gap (V10.31b)
```
★ 전 tier 동일 기준 — 15m ATR 구간별 선택:
  ATR < 0.30% → 0.2% (저변동: XRP)
  ATR < 0.75% → 0.3% (정상: ADA, DOT, SOL)
  ATR ≥ 0.75% → 0.5% (고변동: FET, ORDI)

T1 TP trail과 T2+ TRIM trail과 TRAIL_ON 모두 동일 기준 사용.
기준: HARD_SL_ATR_BASE × 2 / × 5 경계
```

## 수정 시 체크
- [ ] trail_qty가 p["amt"] 전량인지 (부분 수량 금지)
- [ ] step=1 조건 유지
- [ ] ATR gap 구간이 plan_tp1 / plan_trim_trail / plan_trail_on에서 일관되는지
- [ ] HARD_SL_BY_TIER가 config와 일치하는지
- [ ] plan_force_close 경로 (hedge_engine.py) 수정 시 DD_SHUTDOWN 동결 유지
- [ ] DCA 체결 시 step=0, tp1_done=False, trailing_on_time=None 리셋
