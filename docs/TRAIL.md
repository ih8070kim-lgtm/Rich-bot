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

## V10.31x: HIGH 레짐 임계 대폭 상향

변경: 히스테리시스 임계 (상대 ATR 퍼센타일 기준)
- LOW → HIGH:    0.70 → 0.90 (상위 30% → 10%)
- NORMAL → HIGH: 0.73 → 0.92 (상위 27% → 8%)
- HIGH 유지:     0.67 → 0.85 (상위 33% → 15%)

배경:
- 실측 04-21~22 HIGH 블록 6건 전부 BTC 1h 변동 ≤1%
- "상대 ATR 높음"만으로 HIGH 분류 → 평상시 자주 발동
- 사용자 의도: TRAIL은 "급등/급락 방어용 예외 도구"
- 진짜 폭등 폭락 (상위 10% 극단)만 HIGH 분류 목표

영향받는 로직 (HIGH 시만 활성):
- trim_trail_active (trim → trail 전환)
- URGENCY_DCA (긴급 물타기)
- TP1 trailing 전환 (runner.py:778)

추정 효과:
- HIGH 빈도 14% → 1~2%
- TRAIL 작동은 진짜 급변동 시에만 → 예외 도구로 정상화
- URGENCY_DCA 발동 대폭 축소 (MR 봇에 오히려 유리)

검증 로그:
- `log_system("REGIME_CHANGE", ...)` 전환 시 점수/TF 퍼센타일 기록
- 1~2주 누적 후 HIGH 실제 빈도 + 점수 분포 확인 가능


---

## STOP_SL 라이프사이클 (V11 hf8 [05-05])

### 컨셉
거래소 Stop-Market reduceOnly 주문으로 HARD_SL을 보호. 진입 직후 등록, 1분 주기 reconcile로 좀비 정리.

### 라이프사이클
```
[진입]
1. limit OPEN fill → set_p() → _stop_sl_pending 세팅 (runner._apply_pending_fill)
2. 다음 tick _tick_register_stop_sl:
   a. PRE_CANCEL: 같은 sym 모든 STOP+reduceOnly cancel (좀비/재시작 잔존 정리)
   b. STOP_MARKET 등록 (Binance) — type="market" + params={stopPrice, type:"STOP_MARKET", workingType:"MARK_PRICE"}
   c. p["_stop_sl_oid"] = 새 OID 저장

[amt 변경 (DCA fill)]
1. V11_AMT_GROW 분기 발동 (runner.py:1857)
2. existing.pop("_stop_sl_oid") — stale OID 제거
3. _stop_sl_pending 새 amt로 재세팅
4. 다음 tick _tick_register_stop_sl이 PRE_CANCEL + 새 등록 (1번 흐름 재사용)

[청산]
1. FORCE_CLOSE/CLOSE/TRAIL_ON/TP1 전량체결/T2_DEF_V2 사다리/GHOST_CLEANUP 어떤 경로든
2. clear_position 또는 set_p(None) — p에서 _stop_sl_oid 사라짐 (코드 명시 처리 X)
3. 다음 1분 reconcile: 거래소 STOP+reduceOnly fetch → st 매칭 활성 포지션 없음 → cancel
   [STOP_SL_RECONCILE] LINK/USDT close_side=buy oid=... cancel
```

### 1분 reconcile (`_tick_register_stop_sl` 0번 블록)
- **주기**: 1분 (`_last_stop_sl_reconcile` throttle)
- **API**: `ex.fetch_open_orders()` 단일 호출 (weight 40)
- **필터**: STOP type + reduceOnly (type/info.type 둘 다 체크 — ccxt Binance 호환)
- **매칭 로직**:
  - close_side="buy" → 원래 포지션 SHORT (pos_side="sell") → `st[sym]["p_short"]` 체크
  - close_side="sell" → 원래 포지션 LONG (pos_side="buy") → `st[sym]["p_long"]` 체크
  - amt > 0 활성 포지션 없으면 cancel
- **로그**: `STOP_SL_RECONCILE` (system) + 콘솔 print

### 함정
- **type 체크 결함 (V11 hf7까지 존재, hf8에서 수정)**: `(_oo.get("type") or _oo.get("info",{}).get("type",""))`는 OR 단락 평가라 `type="market"` (truthy) 시 `info.type` 안 봄. ccxt Binance가 STOP_MARKET을 `type="market"` + `info.type="STOP_MARKET"`로 반환할 때 누락 — LINK 좀비 발생 의심 원인.
- **cancel queue 한 번 소비 후 손실 (V11 hf7까지 존재, hf8에서 제거)**: `system_state["_stop_sl_cancel_queue"]`가 한 tick에 처리 + `remaining=[]` 빈 리스트로 교체되는 패턴. cancel API 실패 시 silent except → OID 영원히 손실.
- **포지션 dict 단일 진실 공급원의 한계**: `_stop_sl_oid`가 p에만 저장되면 set_p(None)/clear_position 시 OID 손실. V11 hf8은 거래소 fetch를 진실 공급원으로 사용해 이 의존성 제거.

### 체크리스트 (코드 수정 시)
- [ ] STOP type 체크는 반드시 `("STOP" in _top) or ("STOP" in _info_type)` 패턴 (단락 평가 X)
- [ ] cancel queue 다시 도입하지 말 것 — reconcile이 일원화 처리
- [ ] `_stop_sl_oid` 필드는 등록/V11_AMT_GROW 흐름 추적에만 사용 (cancel 결정에 의존 X)
- [ ] reconcile 주기 변경 시 weight 영향 평가 (1분=40 weight, 30초=80 weight)
- [ ] BC/CB/HEDGE role은 SL 등록 자체 안 함 — `if role != "CORE_MR": continue` 가드 유지


---

## STOP_SL 라이프사이클 (V12 [05-06]) — 봇 시장가 매도

### 컨셉
거래소 STOP_MARKET 등록 폐기. 봇이 매 cycle ROI 검사 → 임계 도달 시 시장가 청산.
좀비 SL 구조적으로 발생 불가능.

### 라이프사이클
```
[봇 startup]
1. 첫 _tick_register_stop_sl 호출 → V12 startup_cleanup
   fetch_open_orders → STOP+reduceOnly 일괄 cancel (V11 잔존 정리)
   system_state["_v12_stop_sl_cleanup_done"] = True
2. 이후 호출은 no-op (즉시 return)

[진입]
1. limit OPEN fill → set_p() → 포지션 활성
2. 거래소 STOP_MARKET 등록 X (V11과 다름)
3. plan_force_close 매 cycle ROI 검사 시작

[정상 운영 (HARD_SL 미도달)]
- plan_force_close: 매 cycle 매 포지션 ROI 계산
- ROI > HARD_SL_BY_TIER[dca_lv] → force_close intent 발급 X
- main loop sleep 2초 → 다시 검사

[HARD_SL 발동]
1. ROI ≤ HARD_SL_BY_TIER[dca_lv] (예: T1=-1.4%)
2. plan_force_close: force_close intent 발급
   reason = "HARD_SL_T1(-1.4%, roi=-1.4%)"
3. execute_intents → ex.create_order MARKET reduceOnly
4. 시장가 체결 → set_p None
5. slippage: V10 baseline -0.51% [실측]

[청산]
- TP1/TRIM/HARD_SL/PTP 등 모든 청산은 봇 주도 시장가/limit
- 좀비 SL 발생 가능성 0 (등록 자체 안 했음)
```

### plan_force_close가 처리하는 케이스 (hedge_engine.py:57)
- HARD_SL_T1 (-1.4%) / HARD_SL_T2 (-3.0%)
- T2_DEFENSE_LADDER 사다리 SL/HARD_SL
- T4_HARD_SL (-12.0%, dca_lv≥3 fallback)
- DD_SHUTDOWN (catastrophic 잔고 보호)

### HARD_SL_BY_TIER (V12)
```python
HARD_SL_BY_TIER = {1: -1.4}  # T1만 (T3는 V10.31AO에서 제거)
```

### 위험 — 봇 다운 시
- supervisor/watchdog 재시작 1~3분 의존
- 평상시 다운: 가격 변동 ≤ -1.5%~-3% (slippage 포함) [추정]
- Flash crash + 다운 동시: -10%~-20% tail risk [추정]
- 월 0~1회 빈도 [추정]

### V11 → V12 변경 추적
| V11 (5/4~5/5) | V12 (5/6~) |
|---|---|
| STOP_MARKET 등록 + 1분 reconcile | 등록 폐기, plan_force_close만 |
| `_stop_sl_pending` 3곳 세팅 | 모두 제거 |
| `_stop_sl_oid` 추적 | startup cleanup 1회만 처리 |
| hf1~hf8 8개 hotfix | 1개 변경 (V12) |
| 좀비 패턴 5번 발생 | 구조적 0 |

### 함정
- **활성 포지션 보호 공백**: V12에서 거래소 SL 0이라 봇 다운 시 무방비. supervisor heartbeat 30초 권장.
- **startup_cleanup race**: cleanup이 활성 포지션 정상 SL도 cancel. but V12는 SL 등록 자체 X라 cancel할 \"정상 SL\" 존재 0. 좀비만 정리됨.
- **`_v12_stop_sl_cleanup_done` 영속**: system_state JSON에 저장. 봇 재시작 시 키 사라지면 다시 cleanup 실행 (정상 동작).
- **fetch_open_orders 실패 시 재시도**: 플래그 풀림 → 다음 호출에 다시 시도. rate limit 일시 실패 안전.

### 체크리스트 (코드 수정 시)
- [ ] V12에선 `_stop_sl_pending` 세팅하지 말 것 — 의미 없는 키
- [ ] `_stop_sl_oid` 필드 사용은 V11 잔존 정리(pop)에만 — 새로 세팅 X
- [ ] STOP_MARKET 등록 코드 다시 도입하지 말 것 — 좀비 인프라 5번 실패 패턴
- [ ] `plan_force_close` HARD_SL 로직(hedge_engine.py:361~390)은 보호 핵심 — 함부로 수정 X
- [ ] 봇 다운 보호 강화하려면 거래소 SL이 아닌 watchdog/heartbeat 강화로
