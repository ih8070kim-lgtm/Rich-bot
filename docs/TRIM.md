# TRIM / TP1 — 체크리스트

## ★ V10.31AN-hf1: T2 디펜스 재활성 + T3 사다리 재설계 [04-30]

### 사용자 결정 [04-30]
"DCA 간격 너무 좁혀서 T3 급행열차다. 다시 뒤로 좀 밀고 T2 디펜스도 재적용, T3 디펜스도 재설계".

### T2 디펜스 재활성
hf-16에서 비활성된 `calc_dynamic_trim_thresh` T2 분기 부활.
- 트리거: T2 worst ≤ -2.0% (T2_DEF_WORST_ENTER 기존 값 그대로)
- 효과: TRIM 임계 기본 +1.0% → +0.5% (약반등 trim 허용)
- 의미: T2 단계도 보호 부활 — T3 도달 전 약반등에서 빠른 탈출

### T3 사다리 재설계 (사용자 명시)
```
worst -3.5% → exit  0.0% TRIM  (T3 사이즈만 부분 청산)
worst -4.0% → exit -0.5% TRIM
worst -4.5% → exit -2.0% TRIM
worst -5.0% → exit -3.0% SL    (전량 컷)
worst -5.5% → exit -4.5% SL
worst -6.0%             HARD_SL (즉시 컷)
```

### 변경 의도
- T3 DCA 거리 -2.0 → -3.0% (DCA.md 참조) → T3 진입 자체가 늦어짐
- 따라서 디펜스 시작점도 -2.0 → -3.5% 후퇴
- 첫 3단계 TRIM (회복 기대 영역) → 마지막 3단계 SL/HARD_SL (손실 cap)
- 마지막 -6.0 HARD_SL → HARD_SL_T3(-10) 사이 4%p 공백 의도 (HARD_SL이 안전망)

### 회복 폭 ↑ 인지 [필수 고지]
새 사다리는 **기존보다 회복 폭이 큼**:
| worst | exit | 회복 필요 |
|---|---|---|
| 기존 -2.0 | 0% | 2.0%p |
| 신규 -3.5 | 0% | 3.5%p |
| 기존 -2.5 | -0.5% | 2.0%p |
| 신규 -4.0 | -0.5% | 3.5%p |

→ TRIM 발동 어려움 ↑. 변동성 약한 시기엔 SL/HARD_SL 단계로 진행 가능성 ↑.

### bal=0 fallback 가드 (OP 04-29 22:00 케이스)
DCA.md 참조. TRIM 경로 (`runner.py:1817`)와 DCA fill 경로 (`runner.py:1635`)에 `_bal_trim/_bal_dca <= 0`일 때 보정 skip + `[TRIM_TIER_SKIP]/[DCA_TIER_SKIP]` 로깅.

### PTP 차단 정책 (변경 없음)
`_ptp_active_syms` 활성 시 본 사다리 차단 — PTP 우선 (V10.31AJ).

### 시뮬 [실측 OP 케이스]
**OP 04-29 22:00 trim 시점 snapshot.real_balance_usdt=0 가정**:
- 가드 적용 전: dca_level=2 → 1로 강등 stuck → 다음 28h 동안 1로 stuck → 종국에 hf-17 (D)가 catch
- 가드 적용 후: dca_level=2 유지 → T3 디펜스 사다리 정상 평가 가능 → 새 사다리 -3.5 시작이라 OP 케이스(close ROI -3.09%)에선 사다리 미발동 → HARD_SL_T2(-3.0%)로 cut (변경 없음)

### 체크리스트
- [ ] 새 T3 사다리 worst 임계 코드 일치 (config.py:129~137)
- [ ] T2 디펜스 calc_dynamic_trim_thresh 활성 (config.py:204)
- [ ] T2_DEF_ENTER 로깅 활성 (runner.py:3148)
- [ ] bal=0 가드 양 경로 적용 (runner.py:1635, 1817)
- [ ] HARD_SL_T3=-10.0% 그대로 — 사다리 -6.0 HARD_SL 후 안전망

### 롤백
config.py:129~137 T3_DEFENSE_LADDER 기존 값 복원 + L204 calc_dynamic_trim_thresh T2 분기 다시 주석 처리

---

## ★ V10.31AM3 hotfix-4: T3 다단계 디펜스/SL 사다리 [04-27]

### 컨셉
사용자: "모든 구간에서 조금 반등해도 탈출 가능한 시나리오". T3 진입 후 worst 깊이 비례 빠른 탈출 — 깊이 갈수록 회복 기대치 ↓ → 작은 회복(또는 즉시)에서도 cut.

### 사다리 (config.T3_DEFENSE_LADDER)
```
worst 도달  → ROI 임계   액션
─────────────────────────────────
-2.0%       → 0%         TRIM (T3 사이즈만, T3→T2 복귀)
-2.5%       → -0.5%      TRIM
-3.0%       → -1.5%      SL (전량)
-3.5%       → -2.2%      SL (전량)
-4.0%       → -3.0%      SL (전량)
-4.5%       → 즉시       HARD_SL (전량)
```

### 작동 (planners.py plan_t3_defense_v2)
- 매 tick T3 포지션 순회
- `calc_t3_defense_action(worst, max_roi)` → matched 단계 (mode, exit_roi)
- HARD_SL: max_roi 무관 즉시 발동
- SL/TRIM: current_roi >= exit_roi 도달 시 발동
- TRIM은 `calc_trim_qty(amt, tier=3)` → T3 사이즈만 부분 청산, target_tier=2
- SL/HARD_SL은 `force_market=True` CLOSE intent → 시장가 즉시 컷

### 중복 발동 방지
포지션 metadata `_t3_def_v2_last_step`에 가장 깊은 발동 단계의 worst_enter 저장. 같은 또는 더 얕은 단계 재발동 차단. HARD_SL은 예외 (무조건 발동).

### PTP 차단 (사용자 결정 1.B)
`system_state["_ptp_active_syms"]` 활성 심볼 → 본 함수에서 skip. PTP가 portfolio 일괄 청산 우선. T3 다단계는 **PTP 미발동 시기에만 작동** — 04-22~24 같은 PTP 미정상 시기 보호용.

### 기존 디펜스 모드와의 관계
- **기존 T3_DEF_M5_*** (config.py:122-123, hedge_engine.py:300대): worst≤-5% + ROI -0.5% trim. **상수/코드는 유지** (호환). 새 사다리가 더 얕은 단계(-2.0%)부터 작동하므로 사실상 새 사다리가 우선 발동.
- **HARD_SL_BY_TIER[3]=-10%** (config.py:215): 최후의 보루. 새 사다리 -4.5% HARD_SL이 먼저 발동되어 도달 거의 안 됨 [추정].

### 검증 가능 로그
```
[T3_DEF_V2] ✂ {sym} {side} TRIM qty={qty} roi=+0.0% worst=-2.1% (T3→T2)
[T3_DEF_V2] ⛔ {sym} {side} SL qty={qty} roi=-1.5% worst=-3.1%
[T3_DEF_V2] ⛔ {sym} {side} HARD_SL qty={qty} roi=-X.X% worst=-4.6%
```

### 한계
- 4일치 시뮬 표본 작음 (T3 9 FC만)
- max_roi 컬럼이 양수만 기록 → 음수 임계(-0.5/-1.5 등) 도달 추적 어려움 → current_roi로 호출부에서 체크
- PTP 정상 환경에선 T3 도달 자체 적어 효과 측정 어려움
- 기존 hedge_engine T3_DEF 로직과 양립 — 두 시스템 동시 작동 시 동작 검증 필요

### 수정 시 체크
- [ ] T3_DEFENSE_LADDER 변경 시 calc_t3_defense_action 동작 단위테스트
- [ ] _t3_def_v2_last_step DCA 체결 시 리셋 (현 미구현, T3 재진입 케이스 발생 시 추가)
- [ ] BC/CB 제외 (role 체크 — `CORE_MR`/`CORE_MR_HEDGE`만)
- [ ] PTP exclude 체크 (`_ptp_active_syms`)

---

## ★ V10.31AM: 잔량 float 오차 방어 (OP 68회 루프 해결)

### 증상
TP1/TRIM 전량 체결 후 `amt` 에 float 오차 잔량 남음 (예: `0.0999999999994543` vs min_qty 0.1).
hedge_engine의 RESIDUAL_CLEANUP이 시도하지만 노셔널 $5 미만 → 거래소 MIN_NOTIONAL 거절 → 무한 반복.

### 2중 방어
**1. 발생 원천 차단** (`runner.py:1677`)
```python
_new_amt = max(0.0, float(p.get("amt", 0)) - filled_qty)
# float 오차 흡수 — 최소 수량 절반 미만이면 전량 체결로 간주
if 0 < _new_amt < min_qty * 0.5:
    _new_amt = 0.0
p["amt"] = _new_amt
```

**2. 잔량 강제 클리어** (`hedge_engine.py:141~`)
```python
# 기존: _res_below_min만 체크 → 5분 쿨다운
# AM: min_qty OR MIN_NOTIONAL 미달 시 즉시 clear_position
_res_below_min_qty = _res_amt < _res_min_qty * 0.9999
_res_below_min_notional = _res_notional < 5.0  # Binance 기본
if (_res_below_min_qty or _res_below_min_notional) and _res_amt > 0:
    clear_position(st, symbol, p.get("side", ""))
    log_system("RESIDUAL_FORCE_CLEAR", ...)
    continue  # 이 틱 skip
```

### 효과
- TP1/TRIM 후 찌꺼기 amt 원천 차단
- 기존 찌꺼기 포지션(배포 전)은 첫 틱 내 RESIDUAL_FORCE_CLEAR로 완전 제거

---

## 함정
- ★ V10.31b: 전 tier trail 통합 — T1/T2/T3 모두 동일한 trail 메커니즘
- ★ V10.31c: `_manage_tp1_preorders`는 **LOW/NORMAL 레짐에서 활성 유지** (runner.py:2628에서 호출 중). V10.31b의 "선주문 시스템 전면 제거" 기재는 틀렸음 — 실제로는 HIGH에서만 trail 사용, LOW/NORMAL은 TP1 선주문 유지
- ★ V10.31g: **T3 trim은 레짐 불문 LIMIT 선주문 경로**. plan_trim_trail의 trail 모드는 T2 전용으로 축소
- ★ V10.31j: **worst_roi 기반 동적 TRIM 임계** — T2 worst≤-2.0 → 0.5, T3 worst≤-5.0 → -0.5 (`calc_dynamic_trim_thresh`)
- plan_tp1은 T1 전용 (trail → partial close → step=1 → TRAIL_ON)
- plan_trim_trail은 T2 전용 (★ V10.31g: T3 제외) — trail → trim → tier 감소

## V10.31j 동적 임계 (디펜스 모드)
```
기본 (worst>-2 for T2, worst>-5 for T3):
  T2: 1.0% (TRIM_BLENDED_ROI_BY_TIER[2]) ← ★ V10.31AM3 hotfix-3: 1.5 → 1.0 복원
  T3: 0.5% (TRIM_BLENDED_ROI_BY_TIER[3])

디펜스 (worst 통과 시):
  T2 worst≤-2.0 → TRIM 임계 0.5 (약반등 포획)
  T3 worst≤-5.0 → TRIM 임계 -0.5 (약손실 탈출)

자동 재배치:
  _place_trim_preorders의 EP 검증 블록이 worst 변화도 자동 감지
  calc_trim_price(ep, side, tier, worst_roi) 시그니처 — worst 전달 시
  _v_correct 가격이 변동되어 기존 LIMIT과 0.1% 차이 → 취소 + regen 재배치

로깅:
  T2_DEF_ENTER / T3_DEF_M5_ENTER (log_system) — 포지션당 1회만
  DCA 체결 시 _t2_def_logged / _t3_def_m5_logged 플래그 리셋
  worst_roi_seen 컬럼 (trades.csv 21번째) — 디펜스 임계 재튜닝용
```

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
  ROI ≥ threshold (★ V10.31j: 동적 — 기본 1.5, worst≤-2면 0.5) → trail 활성화 → 시장가 TRIM

■ T2 LOW/NORMAL (_place_trim_preorders)
  DCA 체결 시 calc_trim_price → limit 선주문
  기존 T2 포지션도 자동 재생성 (regen)
  worst 구간 전환(≤-2) 시 EP 검증 블록이 감지 → 취소+재배치 (V10.31j)
  가격 도달 → 체결 → tier 감소 (T2→T1)

■ T3 ALL regime (★ V10.31g) (_place_trim_preorders)
  threshold +0.5% (★ V10.31j: worst≤-5면 -0.5로 하향) — HIGH trail로 가면 변동성에 0.2% 이익만 먹고 이탈 → 재T3 → 강제청산 패턴
  LIMIT으로 +0.5% 정확 도달 시 maker(0.02%)로 깔끔히 T2 감소

■ 전환 안전장치
  T2: LOW→HIGH: 선주문 취소 → trail 시작 / HIGH→LOW: trail 리셋 → 다음 틱 선주문 배치
  T3: 레짐 전환에 영향 받지 않음 — 항상 LIMIT 경로 (V10.31g)
       기존 HIGH+T3 trail 활성 잔존 시 plan_trim_trail 진입부에서 자동 정리
  DCA: 둘 다 리셋 + 디펜스 플래그 리셋 (V10.31j)
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
- [ ] ★ V10.31j: DCA 시 _t2_def_logged / _t3_def_m5_logged 리셋
- [ ] ★ V10.31j: calc_trim_price 호출부 3곳(초기/EP검증/regen)에 worst_roi 전달
- [ ] ★ V10.31j: log_trade 호출부 3곳(TP1/TRIM/FORCE_CLOSE)에 worst_roi_seen 전달
- [ ] trim 수량이 노셔널 기반(calc_trim_qty)인지
- [ ] trim 후 dca_level 정확히 감소
- [ ] T1 TP1 intent에 force_market: True 포함
- [ ] T2 TRIM intent에 force_market: True 포함 (T3은 LIMIT이므로 force_market 없음)
- [ ] GHOST_CLEANUP 시 거래소 DCA/trim limit 취소 (runner.py sync)
