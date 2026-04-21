# TREND — 체크리스트

## 함정
- NOSLOT은 pending 구조 금지 — MR fill이 없으므로 intents.append 즉시 발사
- _noslot_best는 글로벌 최고 score 1개만. 루프 내 즉시 발사하면 다수 동시 진입
- TREND 포지션은 role=CORE_MR — 슬롯 카운팅에 포함됨 (BC/CB와 다름)
- ★ V10.30: score cap 5.0 — abs(score) > 5.0이면 과열로 판단, 진입 차단
- ★ V10.30: TREND_COOLDOWN_SEC = 0 — _open_dir_cd(10분)가 실질 제약
- ★ V10.30: trigger_side=None 시 반드시 continue (side=None crash 방지)
- ★ V10.31b: score 1.0~2.0 밴드는 발사 직전 필터로 블록 (애매한 트렌드)
- ★ V10.31c: **실제 후보 풀 진입 기준은 `_TR_MIN=0.5` 하드코딩** (planners.py 내부). `TREND_MIN_SCORE` config는 미사용 죽은 코드였음 — V10.31c에서 제거
- ★ V10.31c: TREND_SCORE_SKIP 로그는 모듈 dict `_TREND_SKIP_LOG_CD`로 심볼당 5분 1회 제한 (setattr 방식 무효화 수정)
- ★ V10.31i: **COMP는 스큐 예방 도구, 주 수입원 아님** — 가드 `_tr_opp_slots < _sig_side_slots` 적용. 균형/opp우세 상태에서 발사 차단
- ★ V10.31i: NOSLOT A 조건(`opp+1 < sig`)과 COMP 조건(`opp < sig`)의 수식 차이 주의 — NOSLOT은 단독 발사(opp만 +1), COMP는 MR과 동시 발사(양쪽 +1)라 기준선 다름

## 수정 시 체크
- [ ] NOSLOT이 intents.append 사용
- [ ] _noslot_best가 루프 종료 후 1건만 발사
- [ ] _open_dir_cd 쿨다운 적용
- [ ] HEDGE_SIM 기록이 정상 작동하는지
- [ ] score cap (TREND_MAX_SCORE) 체크가 양쪽 NOSLOT 탐색에 적용되는지
- [ ] trigger_side=None 블록 끝에 continue 유지
- [ ] ★ V10.31i: COMP 스큐 가드 `_tr_opp_slots < _sig_side_slots` 체크가 `_tr_opp_slots >= MAX_MR_PER_SIDE` 이후 elif로 배치되어 있는지
- [ ] ★ V10.31i: COMP 스킵 로그 키가 NOSLOT A 조건과 겹치지 않는지 (`COMP_SKIP_SKEW:` vs `NOSLOT_A:`)

## HEDGE_SIM 중간형 시뮬 (★ V10.31e-6)

### 목적
TREND 실제 진입 vs 가상 MR 헷지(= MR 시그널 반대 방향 = TREND와 같은 방향) 병렬 비교.
DCA 트리거까지 시뮬 → "어느 전략이 실제로 돈 벌었나" 검증.

### 기록 시점 (planners.py:1187~)
MR 시그널 발생 → TREND_COMP 후보 발견 → TREND 발사 예정인 순간:
- `_hsim[f"{mr_sym}:{mr_side}"]` = 가상 헷지 포지션 메타 저장
- notional = TREND T1 notional (동일 사이즈 비교)
- 방향 = MR 시그널 반대 (= TREND와 같은 방향)

### 시뮬 진행 (runner._tick_hedge_sim, 매 틱)
- 현재가 기준 가상 ROI 계산 (블렌디드 평단, LEVERAGE 반영)
- DCA 트리거: `DCA_ENTRY_ROI_BY_TIER = {2: -1.8, 3: -3.6}` 도달 시 평단 압축
- DCA 사이즈: `DCA_WEIGHTS = [33, 33, 34]` 비율 그대로
- 종료: ROI ≥ TP1_FIXED[1] (+2%) → `VIRTUAL_TP1`
         ROI ≤ HARD_SL_BY_TIER[3] (-10%) at tier≥3 → `VIRTUAL_HARD_SL`

### 로그 (log_hedge_sim.csv)
14컬럼. 종료 시 1행 기록. 실전 PnL과 병렬 분석 가능.

### 실전 영향
전혀 없음. 읽기 전용 + 자체 state 관리 + try/except 감쌈.
시뮬 실패 시 조용히 skip.

