# Trinity V10.30 — AI 제어 규칙

## 필수 작업 규칙
- 함수 삭제/이동 전 `grep -rn "함수명" --include="*.py"` 실행하여 외부 참조 확인
- `apply_order_results` 등 시그니처 변경 시 모든 호출부 동시 수정 (grep으로 확인)
- 수정 완료 후 `python3 -c "import ast; ast.parse(open('파일').read())"` 문법 검증
- BC/CB는 **모든** 슬롯 카운팅에서 제외 (slot_manager, planners._core_long/short, _HEDGE_ROLES_SLOT)
- DCA 처리 시 trim_trail_active / trim_trail_max 리셋 + stale tp1 필드 정리
- trail close는 p["amt"] 전량 — 잔량 남으면 바이낸스 유령 포지션 발생
- pending 구조는 fill 이벤트 없으면 영원히 발사 안 됨 → 즉시 발사 필요 시 intents.append
- **★ V10.30: .md 파일 읽고 수정 후 반드시 업데이트**
- **★ V10.30: DCA는 단일 경로(_place_dca_preorders LIMIT만). plan_dca 호출 금지**
- **★ V10.30: DCA 주문 전 calc_tier_notional - 현재보유 검증 필수 (과주문 방지)**
- **★ V10.30: FC/TRAIL_ON 시 거래소 잔존 주문 즉시 취소 (_FC_EXCHANGE_CANCEL)**
- **★ V10.31b: 미장전 포지션 정리. ET08:00 진입차단 → ET08:30 전포지션 시장가정리. DST자동, 주말/공휴일 스킵**

## 모듈별 상세 문서 (관련 수정 시 반드시 참조)
- 슬롯/리스크 수정 → `docs/SLOTS.md`
- 신규 진입/MR 수정 → `docs/OPEN.md`
- DCA 수정 → `docs/DCA.md`
- Trim/TP1 수정 → `docs/TRIM.md`
- Trail/청산 수정 → `docs/TRAIL.md`
- BC(Beta Cycle) 수정 → `docs/BC.md`
- CB(Crash Bounce) 수정 → `docs/CB.md`
- TREND 수정 → `docs/TREND.md`
- 유니버스/데이터 수정 → `docs/UNIVERSE.md`
- 전체 아키텍처 → `ARCHITECTURE_V10.26.md`

## 파일 수정 금지 목록
- `execution_engine.py` — 주문 실행 코어 (잘못되면 실주문 사고)
- `position_book.py` — 포지션 상태 (잘못되면 유령 포지션)
- `telegram_deploy_bot.py` — 배포 인프라

## 버그 히스토리 (재발 방지)
| 날짜 | 버그 | 근본 원인 | 체크 대상 |
|------|------|-----------|-----------|
| 04-12 | LINK trim 영구 차단 | DCA 시 tp1_limit_oid 미클리어 | strategy_core.py DCA 블록 |
| 04-12 | TREND_NOSLOT 미발사 | pending 구조 + MR fill 없음 | planners.py trigger_side=None 블록 |
| 04-12 | BC가 MR 슬롯 점유 | _core_short에 BC 미제외 | planners.py line 613 |
| 04-12 | _wider_regime 크래시 | 함수 삭제 후 외부 import 미확인 | strategy_core.py DCA import |
| 04-12 | TREND 3개 동시 진입 | NOSLOT 쿨다운 누락 | _open_dir_cd 적용 |
| 04-13 | T3 market DCA trim 미배치 | strategy_core에 trim_to_place 미세팅 (HIGH 레짐 market DCA만 발생) | strategy_core.py DCA 블록 |
| 04-13 | 죽은 코드 정리 | TP2_PCT/TP2_PARTIAL_RATIO/TRIM_PREORDER_ROI 미사용 | config.py, planners.py import |
| 04-13 | trail max_roi 2.0 미만 발동 | TP1 체결 시 max_roi_seen을 snapshot ROI로 덮어씀 | strategy_core.py TP1 블록 |
| 04-13 | 스큐 로직 전면 제거 | TREND 진입이 스큐 해소 담당. heavy TP인하/DCA가속/T3방어/SKEW_E30 삭제 | planners/config/runner |
| 04-13 | 429 Rate Limit | balance 3초/ticker 2초 캐시 + OHLCV 15초 + sem6 + 429 백오프 | market_snapshot.py |
| 04-13 | BC/CB 동기 fetch 블로킹 | bc_on_tick/cb_on_tick을 asyncio.to_thread로 감싸기 | runner.py |
| 04-13 | DCA 선주문 도입 | T1 진입 시 T2 limit 선배치, DCA fill 시 다음 tier 배치 (maker 수수료) | runner/planners/strategy_core |
| 04-14 | side=None crash | trigger_side=None 시 continue 누락 → ccxt crash | planners.py NOSLOT 블록 |
| 04-14 | DCA 후 ghost trailing | runner DCA fill 시 step/tp1_done 미리셋 → 트레일+TRIM 차단 | runner.py DCA fill handler |
| 04-14 | RESIDUAL 무한루프 | float epsilon(2.84e-14) + reduce_fail_cooldown 무시 | hedge_engine.py |
| 04-14 | FC 후 DCA 좀비 | FC 시 거래소 LIMIT 미취소 → 빈 슬롯에 DCA 체결 | strategy_core.py + runner.py |
| 04-14 | BC 즉사 | 1h excess 뜨거운데 1d 정규화만 보고 진입 → REHEAT 즉사 | beta_cycle.py baseline 도입 |
| 04-14 | T2 정규 TP1 빠짐 | T2+에서 trim 미발동 시 정규 TP1 fallback → 전량 사망 | planners.py continue 추가 |
| 04-14 | DCA 과주문 (AAVE 3배) | 목표 노셔널 검증 없이 개별 weight 기반 주문 | planners.py + runner.py 가드 |
| 04-14 | DCA 이중 경로 | plan_dca(시장가) + DCA_PRE(LIMIT) 동시 발동 | plan_dca 제거, 단일 경로 |
| 04-15 | T2+ trim trail 미발동 | plan_tp1 guard(tp1_preorder_id/limit_oid/step 등)가 T2+ trail까지 차단 | V10.31b: 전 tier trail 통합, preorder 시스템 제거 |
| 04-15 | TIA T2 포지션 비대화(3728) | GHOST_CLEANUP이 거래소 DCA limit 미취소 → 다음 OPEN에 옛날 DCA 체결 | V10.31b: GHOST_CLEANUP 시 PENDING_LIMITS+dca_preorders 취소큐 추가 |
| 04-15 | trim이 limit으로 나감 | plan_trim_trail에 force_market 미설정 → limit placed → sync 불일치 → ghost | V10.31b: plan_trim_trail metadata에 force_market:True 추가 |
| 04-15 | 재시작 후 stale DCA 잔존 | _PENDING_LIMITS 유실 + dca_preorders OID만 남음 → stale pop만 하고 거래소 미취소 | V10.31b: stale 감지 시 ex.cancel_order 호출 + GHOST에 FC_EXCHANGE_CANCEL 추가 |
| 04-15 | calc_roi_pct UnboundLocalError | TRAIL_ON 블록 내 로컬 import가 TP1 블록까지 오염 → TP1 체결 시 크래시 → 롱 소실(L2→L0) | V10.31b: apply_order_results 함수 최상단으로 import 이동 |
| 04-13 | RESIDUAL_CLEANUP 무한루프 | hedge_engine이 reduce_fail_cooldown 체크, runner는 exit_fail_cooldown 세팅 → 필드명 불일치 → 쿨다운 무시 | V10.31b: exit_fail_cooldown_until로 통일 |
| 04-15 | SEI CorrGuard -5.3% 조기컷 | corr<0.5+ROI<-4% → 강제청산이 T3 회복 차단 | V10.31b: CorrGuard 제거 (pure trim/trail 신뢰) |
| 04-15 | Zombie 강제청산 | 슬롯풀 시 T1/T2 조건부 청산 → 회복 차단 | V10.31b: Zombie 로직 제거 |
| 04-15 | TREND_COMP 쿨다운 막힘 | DOT TRAIL_ON 15분 후 SEI MR이 DOT를 companion 선택 → DOT 쿨다운에 걸려 REJECT | V10.31b: entry_type=TREND는 쿨다운 면제 |
| 04-17 | trim 선주문 가격 stale | DCA로 EP 변경 후 기존 trim limit 가격 미갱신 → 구 EP 기준 +3.3%에서 대기 (정상 1.5%) | V10.31b: _place_trim_preorders에서 매 틱 가격 검증, 0.1% 이상 차이 시 취소+재배치 |
