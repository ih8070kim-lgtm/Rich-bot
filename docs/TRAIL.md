# TRAIL — 체크리스트

## 함정
- trail close는 반드시 p["amt"] 전량. 부분 수량 사용 시 유령 포지션 발생
- step < 1이면 trailing 진입 안 됨 — TP1 체결로 step=1 전환 필수
- ★ V10.31b: TP1도 trail 방식 — tp1_preorder/tp1_limit_oid 없음
- TP1 체결 시 max_roi_seen을 snapshot ROI로 덮어쓰면 안 됨 — 반드시 max(현재ROI, 기존max) 사용
- ★ V10.31s: **BTC 대비 이탈률 관측 로그** — 청산 로직 아님, 데이터 수집용. 진입 시점 `_btc_entry_price` 저장하고, 매 tick마다 BTC 대비 alt 이탈률 계산. 임계 |adverse| ≥ 1.0%p + 포지션당 5분 쿨다운으로 `log_system.csv`에 `BTC_DECOUPLE_OBS` 태그 기록. FORCE_CLOSE 발생 시 `BTC_DECOUPLE_CLOSE` 태그로 최종 이탈률 기록. 목적: 실손실 케이스와 이탈률 상관 분석 → 임계 근거 확보 후 V10.32+에서 실제 청산 로직 판단.

## BTC_DECOUPLE 관측 로그 (V10.31s)
```
정의:
  adverse_excess = 봇 방향 기준 BTC 대비 불리 이탈률
    숏 포지션: alt_pct - btc_pct (양수 = 알트가 BTC보다 상승=숏 불리)
    롱 포지션: btc_pct - alt_pct (양수 = 알트가 BTC보다 하락=롱 불리)

로그 조건 (OBS):
  duration ≥ 15분 (초기 노이즈 제외)
  |adverse_excess| ≥ 1.0%p
  포지션당 5분 쿨다운 (스팸 방지)
  필드: sym side roi dur alt_pct btc_pct adverse_excess

로그 조건 (CLOSE):
  FORCE_CLOSE 발생 시 최종 이탈률 무조건 기록
  필드: sym side roi dur alt_pct btc_pct adverse_excess reason

실측 ARB 04-22 청산 기록 예시:
  BTC_DECOUPLE_CLOSE ARB/USDT sell roi=-12.00% dur=75min 
    alt=+3.80% btc=+0.21% adverse=+3.59%p reason=T3_DEF_SL(...)

분석 활용:
  1주 데이터 축적 후 손실 크기 vs adverse 상관 분석
  임계 결정 근거 확보 후 V10.32에서 실제 청산 조건 설정
```

## Trail gap (V10.31b)
```
★ 전 tier 동일 기준 — 15m ATR 구간별 선택:
  ATR < 0.30% → 0.2% (저변동: XRP)
  ATR < 0.75% → 0.3% (정상: ADA, DOT, SOL)
  ATR ≥ 0.75% → 0.5% (고변동: FET, ORDI)

T1 TP trail과 T2+ TRIM trail과 TRAIL_ON 모두 동일 기준 사용.
기준: HARD_SL_ATR_BASE × 2 / × 5 경계
```

## Pre-Market Clear (V10.31b)
```
★ 미장 오픈 전 포지션 정리 — T3 FC 손실 36~45% 절감
  ET 08:00: 신규 진입 차단 (_pmc_block_entry)
  ET 08:30: 전 포지션 시장가 정리 (PRE_MKT_CLEAR, 1회)
  ET 09:30: 진입 차단 해제
  DST 자동 반영 (zoneinfo America/New_York)
  주말(토/일) + NYSE 2026 공휴일 스킵
  DCA 선주문 취소큐 추가
```

## 수정 시 체크
- [ ] trail_qty가 p["amt"] 전량인지 (부분 수량 금지)
- [ ] step=1 조건 유지
- [ ] ATR gap 구간이 plan_tp1 / plan_trim_trail / plan_trail_on에서 일관되는지
- [ ] HARD_SL_BY_TIER가 config와 일치하는지
- [ ] plan_force_close 경로 (hedge_engine.py) 수정 시 DD_SHUTDOWN 동결 유지
- [ ] DCA 체결 시 step=0, tp1_done=False, trailing_on_time=None 리셋
