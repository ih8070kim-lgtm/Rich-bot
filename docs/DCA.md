# DCA — 체크리스트

## ★ V10.31AN-hf1: DCA 거리 후퇴 + DCA_ROI_TRIGGERS 정합 [04-30]

### 변경
```
DCA_ENTRY_ROI_BY_TIER = {2: -2.0, 3: -3.0}   # 기존 hf-4 {2: -1.5, 3: -2.0}
```

### 사용자 결정 [04-30]
"T3 급행열차다. 다시 뒤로 좀 밀고 T2/T3 디펜스 재설계". hf-4의 평단 압축 정책이 변동성 시기에 T3 풀로딩 빨라지는 부작용 확인 → DCA 간격 늘려 T3 도달 늦춤.

### 부수 수정 — DCA_ROI_TRIGGERS 정합
**잠재 버그 발견 [04-30]**: `planners.py:431` `DCA_ROI_TRIGGERS = {2: -1.8, 3: -3.6}`이 stale 값. `_build_dca_targets` (TRIM 후 dca_targets 재생성용)가 이 dict 사용 → V10.31AM3 hf-4 배포 후에도 trim 후 재생성 시 stale -1.8/-3.6 적용 → 새 DCA 거리 미반영.

**수정**: `_build_dca_targets`가 `DCA_ENTRY_ROI_BY_TIER` 직접 참조. `DCA_ROI_TRIGGERS`는 deprecated 표기 (호환성 유지).

### bal=0 fallback 가드 (OP 04-29 22:00 케이스)
**[실측] 근본 원인**: `runner.py:1817` TRIM 처리 hf-17 (B) 잔량 기반 보정에서 `_bal_trim=0`이면 `calc_tier_from_amt(amt, price, 0) → return 1`. → `_actual_tier=1, _target_tier(=2)와 불일치 → dca_level=1로 잘못 강등`.

**OP 케이스**: 04-29 22:00 trim_T3 시점 snapshot.real_balance_usdt=0 (일시적 fetch 미완료 추정) → dca_level=1로 stuck → 04-30 00:46까지 유지 → trades.csv도 dca_level=1 기록. 다행히 hf-17 (D) HARD_SL 평가 시 잔량 기반 임계 보정이 catch (그 시점 bal>0).

**수정 위치**:
- `runner.py:1817` TRIM 경로: `if _bal_trim <= 0: skip 보정 + [TRIM_TIER_SKIP] 로깅`
- `runner.py:1635` DCA fill 경로: 동일 가드 + `[DCA_TIER_SKIP]` 로깅

### 시뮬 [실측 OP 케이스]
**가드 적용 전**:
- 22:00 trim 시점 _bal_trim=0 → calc_tier_from_amt → 1
- _target_tier 2→1 강등 → dca_level=1 stuck
- 04-30 00:46 close trades.csv dca_level=1

**가드 적용 후**:
- _bal_trim=0 → skip 보정 → _target_tier=2 유지
- dca_level=2 정상 → 트림 후 단계별 보호 정상 작동

### 한계 [필수 고지]
- bal=0 자체는 다른 이유 (API 일시 실패, fetch 미완료) — 가드는 dca_level 보호만, bal=0 자체는 해결 안 함
- snapshot.real_balance_usdt가 stale 값일 가능성도 있음 — 가드는 0만 체크, stale은 미감지

### 롤백
config.py:108 `DCA_ENTRY_ROI_BY_TIER = {2: -1.5, 3: -2.0}` (hf-4 값) 또는 `{2: -1.8, 3: -3.6}` (V10.29b 원본)

---

## ★ V10.31AM3 hotfix-4: DCA 트리거 밀착 [04-27]

### 변경
```
DCA_ENTRY_ROI_BY_TIER = {2: -1.5, 3: -2.0}   # 기존 {2: -1.8, 3: -3.6}
```

### 근거
사용자 결정: "T2/T3 둘 다 빠르게 들어가서 평단 압축 + 자본 활용 ↑". 데이터 분석 [실측 4일치]:
- T1 익절 케이스 81건 중 worst≤-1% 도달 후 회복한 케이스 15건 (+$37) → 밀착하면 사이즈 ↑로 추가 익절 +$18 [추정]
- T2 디펜스 trim 9건 (+$5.65) → T3 발동 시 +$37 [추정]

### 위험 — 짝으로 작동 가정
T2 -1.5% 좁힘만 하면 추세 케이스 T3까지 풀로딩으로 손실 ↑ [추정]. **T3 다단계 디펜스/SL 사다리(TRIM.md 참조)와 짝으로 도입**해서 T3 도달 시 빠른 탈출로 보호.

### PTP 충돌
DCA 트리거 자체는 PTP와 무관. 단 T3 도달 후 디펜스 사다리는 `_ptp_active_syms` 활성 시 차단 (사용자 결정 1.B). PTP 정상 작동 환경에선 T3 다단계 가치 적음 — 04-22~24 같은 PTP 미정상 시기 재발 방지용.

### 롤백
config.py:108 `DCA_ENTRY_ROI_BY_TIER = {2: -1.8, 3: -3.6}` + L82 `DCA_ENTRY_ROI = -1.8`로 원복.

---

## 함정
- DCA 체결 시 tp1_limit_oid / tp1_preorder_id 미클리어 → trim 영구 차단 (04-12 버그)
- ★ V10.30: plan_dca(시장가) 제거 — _place_dca_preorders(LIMIT)로 단일화
- ★ V10.30: DCA 주문 전 목표 노셔널 대비 부족분만 주문 (과주문 방지)
- ★ V10.31c: **plan_dca 함수 자체도 삭제됨** (V10.30 호출 제거 후 함수 정의만 잔존하던 죽은 코드 276줄)
- T4/T5 코드 잔존하나 DCA_WEIGHTS=[25,25,50] 3티어라 도달 불가 (죽은 코드, 무해)
- ★ V10.31r: **_apply_pending_fill 중복 호출 방지 가드 필수** — order_id 기준 idempotency. `_APPLIED_FILL_OIDS` 모듈 전역 dict로 최근 1시간 처리된 oid 추적. `_manage_pending_limits` 5초 주기 + `remove_pending_limit` race condition으로 같은 체결이 2회 반영되는 버그 실측 (ARB T3 04-22 16:48:40 amt=13101.9 = 의도 2배). 다행히 `_sync_positions_with_exchange` (30초 주기)가 거래소 실제 qty로 보정해줘서 결과적으로 살아남았으나 중간 32초간 책 불일치. 가드로 원천 차단
- ★ V10.31t: **DCA 체결 시 p["time"] 보존** — `_apply_pending_fill`와 `strategy_core.apply_order_results` 둘 다에서 DCA 체결 시 p["time"] = now 덮어쓰기 제거. p["time"]은 OPEN 시각 전용, last_dca_time은 별도 필드. 시간컷(T3_3H/T3_8H)이 OPEN 기준 hold로 올바르게 작동하도록 복원. 실측 ARB 04-22 12:43 OPEN → 12:58 T2 → 16:48 T3, 매번 time 덮어써져 18:03 HARD_SL 도달까지 시간컷 미발동 버그 확인 및 수정.
- ★ **V10.31AD: `max_roi_by_tier` 저장은 `p["dca_level"] = tier` 할당 이전에 pre-값 캡처 필수**
  - 버그 (V10.31e~AC): `_pre_tier = int(p.get("dca_level", 1))`을 할당 **뒤** 읽음 → NEW tier 읽혀서 저장 키 한 칸씩 밀림 → 리더는 항상 `"1"` 조회하는데 라이터는 `"2"`/`"3"`에 저장 → `t1_max_roi_pre_dca` 영구 0.0 (실측 12/12 T2+ 청산)
  - 추가: strategy_core에 중복 저장 블록 존재 → `max_roi_seen=0` 리셋 후 재저장으로 덮어쓰기 (파괴적)
  - 해결: DCA 블록 맨 위에서 `_pre_tier_val`/`_pre_max_val` 지역변수로 캡처 후 사용. strategy_core 중복 블록 삭제.
  - 영향 파일: runner.py:1532-1536, strategy_core.py:251-258
- ★ **V10.31AG: 메인 루프 순서 역전 — pending_fill → SYNC (이중 qty 반영 원천 차단)**
  - 버그 (V10.31AF 이전): DCA 체결이 두 경로로 중복 반영
    - 경로 A: `_sync_positions_with_exchange` → `fetch_positions`로 거래소 전체 스냅샷 → `book_p['amt'] = ex_qty` 덮어쓰기
    - 경로 B: `_manage_pending_limits` → `_apply_pending_fill` → `p["amt"] += filled_qty` 추가
    - 메인 루프가 **SYNC 먼저(L3088) → pending_fill 나중(L3092)** 순서라 같은 DCA 체결이 두 번 적용
  - 실측 04-24 FIL: 거래소 T2 체결(+343.8) → SYNC qty=779.8 덮어쓰기 → 같은 틱 _apply_pending_fill +343.8 → **amt=1123.6 (의도 2배)**. 30초 후 다음 SYNC cycle에서 재조정되지만 그 사이 잘못된 qty로 trim 계산 오염, ReduceOnly -2022 대량 발생
  - 근본 원인: 두 관찰자(SYNC snapshot vs pending_fill event)가 **같은 이벤트 소스(거래소)를 각자 반영**. 역할 분리 부재.
  - 해결: 메인 루프 순서 바꿔 **pending_fill 먼저, SYNC 나중**으로 배치
    - Pending Fill = 1차 관찰자(정확도) → book 업데이트
    - SYNC = 2차 관찰자(완결성) → pending이 놓친 고아만 보정하는 safety net
  - 영향 파일: runner.py:3085-3099 (순서 역전 1곳)
  - 역할 위계 확정: 같은 이벤트의 **단일 진실 공급원(pending fill)** 확립, SYNC는 검증 전용
  - 엣지 케이스 검증: (1) 순수 고아 포지션(pending 없이 거래소만 있음) → SYNC가 여전히 복구 ✓ (2) WebSocket 체결 + pending + SYNC 3중 → `_APPLIED_FILL_OIDS` 가드가 이중 반영 차단 ✓ (3) pending fill 일시 실패 → 다음 SYNC cycle에서 반영 ✓

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
★ V10.31j 추가:
_t2_def_logged → False     # T2 디펜스 활성 플래그 (worst≤-2 최초 로그)
_t3_def_m5_logged → False  # T3 디펜스 활성 플래그 (worst≤-5 최초 로그)
```

## 수정 시 체크
- [ ] 위 필드 클리어가 runner DCA fill 핸들러에 유지되는지
- [ ] DCA 주문 전 calc_tier_notional - 현재보유 검증 (양쪽 경로)
- [ ] ep 계산이 블렌디드 방식 유지되는지
- [ ] **DCA 선주문(dca_preorders)이 DCA fill/trim fill 시 전부 취소되는지**
- [ ] **DCA 선주문이 타임아웃 면제(is_dca_pre)되는지**
- [ ] **plan_dca 호출이 제거되었는지 (generate_all_intents)**
- [ ] **★ V10.31AD: `max_roi_by_tier` 저장 시 `_pre_tier_val`을 `p["dca_level"] = tier` 할당 이전에 캡처했는지 (runner.py + strategy_core.py 양쪽)**
- [ ] **★ V10.31AD: strategy_core.py에 중복 저장 블록(과거 L347-354) 없는지 — 있으면 0.0 덮어쓰기**
