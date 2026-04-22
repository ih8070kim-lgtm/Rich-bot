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
- ★ V10.31j: **TREND T3 3h 컷 (plan_t3_3h_cut_trend)** — hold≥3h부터 단계적 컷. MR T3는 기존 plan_t3_8h_cut(7h~8h) 유지
- ★ V10.31j: TREND/MR 구분은 `entry_type == "TREND"` 조건. plan_t3_8h_cut에 MR only 조건 추가됨
- ★ V10.31q: **NOSLOT/COMP universe 필터 필수** — `snapshot.global_targets_long/short` 외부 심볼은 후보 제외. ohlcv_pool은 stale 데이터 누적 (과거 universe 심볼 복사본), universe 제외된 LINK가 3h51m 후 TREND_NOSLOT으로 진입하는 버그 실측 → 04-22 LINK 04:42 케이스. side별 allowed_pool 매칭 필수 (_tr_opp_side=buy → LONG 풀, sell → SHORT 풀)
- ★ V10.31q: TREND_NOSLOT 발사 로그에 `β={값}` 포함 (snapshot.beta_by_sym 조회). universe 갱신 시에도 log_system.csv `UNIV_LONG_BETA`/`UNIV_SHORT_BETA` 태그로 베타 영구 기록
- ★ V10.31t: **p["time"] OPEN 시각 고정** — DCA 체결 시 덮어쓰지 않음. 이전엔 runner._apply_pending_fill과 strategy_core.apply_order_results에서 DCA 체결마다 `p["time"] = now`로 갱신 → T3_3H/T3_8H 시간컷 hold 계산이 "마지막 DCA 이후" 기준으로 작동하여 OPEN 기준 시간컷 의도가 무력화됨. 실측 ARB 04-22 12:43 OPEN → 16:48 T3 체결 시점에 time 덮어써져 18:03 HARD_SL -12% 도달까지 시간컷 미발동. 수정 후 OPEN 이후 경과 정확히 계산됨. last_dca_time은 별도 유지하여 trim/pending 로직은 영향 없음.
- ★ V10.31u: **TREND_COMP → HEDGE_COMP 전환** — 기존 다른 심볼 반대 방향 추세 추종 제거, 동일 심볼 반대 방향 CORE_MR_HEDGE 동시 진입으로 교체. HEDGE_SIM 04-21~22 13건 100% 승률 +2% 결과를 실전화. MR 진입 성공 시 `_pending_hedge_comp`에 저장 → 다음 tick에서 반대 방향 OPEN intent 발사 (entry_type=TREND, role=CORE_MR_HEDGE). **role 분리**: 기존 CORE_HEDGE(hedge_core 스큐 전용, 비활성)와 구분 위해 CORE_MR_HEDGE로 명명. MR과 동일 DCA/TP1/HARD_SL/T3_3H 시간컷 로직 적용. 슬롯은 CORE_MR과 합쳐서 MAX_MR_PER_SIDE 체크 (slot_manager에 CORE_MR_HEDGE 포함). 바이낸스 hedge_mode positionSide로 동일 심볼 양방향 기술적 가능. 기존 TREND_COMP 심볼 검색 루프 (158줄) 완전 삭제. ARB 타입 큰 손실 원인 제거. 모든 HEDGE 계열 집합(_HEDGE_ROLES_SLOT/_HEDGE_ROLES_U/zombie/urgency 등)에 CORE_MR_HEDGE 없음 → 기존 HEDGE 로직 자동 건너뛰지 않고 MR 로직 적용받음.

## 수정 시 체크
- [ ] NOSLOT이 intents.append 사용
- [ ] _noslot_best가 루프 종료 후 1건만 발사
- [ ] _open_dir_cd 쿨다운 적용
- [ ] HEDGE_SIM 기록이 정상 작동하는지
- [ ] score cap (TREND_MAX_SCORE) 체크가 양쪽 NOSLOT 탐색에 적용되는지
- [ ] trigger_side=None 블록 끝에 continue 유지
- [ ] ★ V10.31i: COMP 스큐 가드 `_tr_opp_slots < _sig_side_slots` 체크가 `_tr_opp_slots >= MAX_MR_PER_SIDE` 이후 elif로 배치되어 있는지
- [ ] ★ V10.31i: COMP 스킵 로그 키가 NOSLOT A 조건과 겹치지 않는지 (`COMP_SKIP_SKEW:` vs `NOSLOT_A:`)
- [ ] ★ V10.31j: plan_t3_3h_cut_trend가 `entry_type=="TREND"` 체크
- [ ] ★ V10.31j: plan_t3_8h_cut에 `entry_type=="MR"` 체크 추가되어 있는지
- [ ] ★ V10.31j: _t3_3h_step 필드가 _t3_8h_step과 별개인지 (중복 방지)
- [ ] ★ V10.31j: is_t3_3h_limit 플래그가 is_t3_8h_limit과 별개인지
- [ ] ★ V10.31q: TREND_NOSLOT 루프 앞에 `_tr_allowed_pool = long_pool if opp=="buy" else short_pool` 세팅되어 있는지
- [ ] ★ V10.31q: TREND_COMPANION 루프에도 동일 universe 필터 적용되어 있는지 (2곳 모두)
- [ ] ★ V10.31q: TREND_NOSLOT intent 발사 시 snapshot.beta_by_sym 조회해서 β 로그 남기는지

## T3 시간 컷 테이블 (★ V10.31j)
```
TREND T3 (plan_t3_3h_cut_trend):
  3h00 step 0: limit +0.5% 유리방향
  3h20 step 1: 이전 취소 + +0.35%
  3h40 step 2: 이전 취소 + +0.20%
  4h00 step 3: 시장가 강제 정리

MR T3 (plan_t3_8h_cut) — 기존 유지:
  7h00 step 0: limit +0.5%
  7h20 step 1: +0.35%
  7h40 step 2: +0.20%
  8h00 step 3: 시장가

실측 근거 (OLD 500건):
  TREND_T3 회복률: <3h 90% / ≥3h 38%
  MR_T3 FC: 전량 >12h (시간 누적 패턴)
  → entry_type별 분리 컷 시간이 합리적
```

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


## V10.31v: 대시보드 HEDGE vs TREND 구분 표시

- 기존: entry_type=TREND 통합 표시 (TREND_NOSLOT + HEDGE_COMP 섞임)
- 변경: `display_type` 필드 추가 — 대시보드에서 별도 카테고리
  - `role=CORE_MR_HEDGE + entry_type=TREND` → "HEDGE" (V10.31u 동일 심볼 반대)
  - `role=CORE_MR + entry_type=TREND` → "TREND" (TREND_NOSLOT 다른 심볼)
  - `entry_type=MR` → "MR"
- `strat_pnl` 집계도 HEDGE 분리 — 7일 누적 PnL 별도 추적
- `_parse_trade_line`에 `entry_type` 필드 추가 (col 15)
- 대시보드 프론트엔드는 `display_type` 사용 권장 (기존 entry_type과 호환 유지)
