# Trinity V10.31e — AI 제어 규칙

## 필수 작업 규칙
- 함수 삭제/이동 전 `grep -rn "함수명" --include="*.py"` 실행하여 외부 참조 확인
- `apply_order_results` 등 시그니처 변경 시 모든 호출부 동시 수정 (grep으로 확인)
- 수정 완료 후 `python3 -c "import ast; ast.parse(open('파일').read())"` 문법 검증
- BC/CB는 **모든** 슬롯 카운팅에서 제외 (slot_manager, planners._core_long/short, _HEDGE_ROLES_SLOT)
- DCA 처리 시 trim_trail_active / trim_trail_max 리셋 + stale tp1 필드 정리
- trail close는 p["amt"] 전량 — 잔량 남으면 바이낸스 유령 포지션 발생
- pending 구조는 fill 이벤트 없으면 영원히 발사 안 됨 → 즉시 발사 필요 시 intents.append
- **★ V10.30: .md 파일 읽고 수정 후 반드시 업데이트**
- **★ V10.30: DCA는 단일 경로(_place_dca_preorders LIMIT만). plan_dca 함수 자체 제거됨 (V10.31c)**
- **★ V10.30: DCA 주문 전 calc_tier_notional - 현재보유 검증 필수 (과주문 방지)**
- **★ V10.30: FC/TRAIL_ON 시 거래소 잔존 주문 즉시 취소 (_FC_EXCHANGE_CANCEL)**
- **★ V10.31b: 미장전 포지션 정리. ET08:00 진입차단 → ET08:30 전포지션 시장가정리. DST자동, 주말/공휴일 스킵**
- **★ V10.31c: ROI 계산은 `v9.utils.utils_math.calc_roi_pct()` 단일 경로 (인라인 계산 금지)**
- **★ V10.31c: SYM_MIN_QTY는 부팅 시 ccxt load_markets에서 동적 채움 (`_load_sym_limits_from_ccxt`). 하드코딩은 fallback**
- **★ V10.31c: precision/notional 에러(`-1111`, `-4003`, `-4005`, "minimum amount precision", "minimum notional")도 exit_fail_cooldown 트리거**

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
- 수익성 지표/로깅 인프라 → `docs/OBSERVABILITY.md` (V10.31d 신규)
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
| 04-17 | TREND score 1.0~2.0 구간 손실 | 애매한 트렌드 세기 → DCA까지 끌려감 → T3 FC | V10.31b: score 1.0~2.0 TREND 진입 차단 (COMP+NOSLOT 모두) |
| 04-17 | BC 활성 시 MR 레버리지 초과 | BC가 잔고 사용 중인데 MR이 전체 잔고 기준 사이징 → 실질 레버리지 초과 | V10.31b: _mr_available_balance() — BC 노셔널 차감 후 MR 사이징 (진입/DCA/trim 전부) |
| 04-17 | PnL 과장 (내부계산 vs 바이낸스) | OrderResult에 realized_pnl 미포함 → 내부 (exit-ep)×qty 계산이 trim 과다매도 등으로 부풀림 | V10.31b: OrderResult.realized_pnl 추가 + order_router/runner에서 바이낸스 trades realizedPnl 추출 |
| 04-17 | trim PnL 3배 뻥튀기 | `filled_qty × price_diff × LEVERAGE` — qty가 이미 레버리지 반영 수량인데 LEVERAGE(3) 추가 곱셈 | V10.31b: `× LEVERAGE` 제거 + realizedPnl 우선 사용 |
| 04-18 | RESIDUAL_CLEANUP 무한재시도 (재발) | `_record_fail_cooldown`이 `-2022/ReduceOnly`만 체크 → Binance precision 에러(`-1111` 등)는 쿨다운 미세팅 → 3초마다 무한재시도. 추가로 SYM_MIN_QTY 하드코딩에 대다수 심볼 누락 → `_res_min_qty=1.0 fallback` → `amt < min*2` 오판으로 의미있는 수량을 dust로 청산 | V10.31c: (a) order_router + runner 이중 방어 — precision/notional 에러군 쿨다운 60초 (b) `_load_sym_limits_from_ccxt` — 부팅 시 ccxt load_markets에서 동적 채움 |
| 04-18 | TREND_SCORE_SKIP 3초 스팸 | `setattr(plan_open, ...)` 방식 쿨다운이 실제 작동 안 함 → 동일 sym+sig 조합 매 틱 로깅 | V10.31c: 모듈 레벨 `_TREND_SKIP_LOG_CD: Dict[str, float]`로 교체 |
| 04-18 | log_skew.csv 죽은 로깅 | 스큐 로직은 V10.30에서 전면 제거되었으나 logger 호출부 잔존 → 969KB/주 누적 | V10.31c: log_skew 함수/호출/schema/config 전부 제거. _urgency_score 계산은 유지 |
| 04-18 | ROI 인라인 중복 | strategy_core._log_pos / runner 포지션 스냅샷에서 `(curr_p-ep)/ep * LEV * 100` 인라인 계산 — calc_roi_pct와 중복, 수정 시 동기화 위험 | V10.31c: 두 위치 모두 `calc_roi_pct()` 호출로 통일 |
| 04-18 | plan_dca 죽은 코드 | V10.30에서 호출 제거되었으나 함수 정의(276줄) 잔존 | V10.31c: 함수 삭제 + docstring 정정 |
| 04-18 | TREND_MIN_SCORE 미사용 config | config.py에 정의되고 import만 되어 있음. 실제 조건 체크는 `_TR_MIN=0.5` 하드코딩 사용 | V10.31c: config 제거 + import 제거 + _calc_trend_score docstring 정정 |
| 04-18 | BTC 방향성 필터 효과 불명 | 로그만 보고 "역방향 MR이 T3 FC 원인"이라 가정했으나 결과론적. 실측 필요 | V10.31c: TREND_FILTER_SIM shadow logging 신설. Strict(1h≤-1.5%/6h≤-4%/dev≤-3%) + Loose(1h≤-0.7%/6h≤-2%/dev≤-1.5%) 두 임계값 병렬 기록. MR 청산 시점에 "필터가 차단했다면 놓쳤을 ROI" 집계. 실전 진입은 차단하지 않음 (shadow only) |
| 04-18 | BC trail 조기 청산 의심 | AAVE 3.5%/FIL 3.6% 마감 — activation 3%/floor 1.5% 설계 예민함 체감. 근데 peak_roi 미로깅이라 실제 giveback 측정 불가 | V10.31c: (a) TRAIL 평가에서 `h_1h` wick 제거 — trail은 "추세 꺾임 확인"이므로 wick 스파이크로 발동하면 노이즈 과민반응. SL은 wick 유지(손실 방어). (b) `bc_peak_roi` 추적 + `BC_EXIT` 로그에 exit/peak/giveback 기록 — 2~3주 데이터 수집 후 activation/floor 재검토 |
| 04-18 | 슬롯 5/5 → 4/4 | MAX_LONG/SHORT=5로 되어 있으나 실운영은 MAX_MR_PER_SIDE=4에 막혀 4/4였음. 설정과 실제 불일치 | V10.31c: MAX_LONG/SHORT 4/4, TOTAL_MAX_SLOTS 10→8. 논리 변화 없음, 명시화만 |
| 04-18 | 선주문 취소 로직 위치 혼란 | `_cancel_tp1_preorder` / `_cancel_trim_preorders`가 runner.py에 있고, 거래소 주문 취소 API 호출인데 실행 책임이 runner에 분산됨 | V10.31c: 구현체를 `v9/execution/order_router.py`로 이동 (`cancel_tp1_preorder`, `cancel_trim_preorders`). runner.py는 wrapper 유지하여 외부 호출 호환. ~60줄 runner.py에서 제거 |
| 04-18 | 대시보드 경로 불일치 | status_writer.py가 `v9/v9_status.json` 쓰고 status_server.py가 `프로젝트루트/v9_status.json` 읽음. 서로 다른 파일 → 대시보드 한 번도 작동 안 함 | V10.31c: status_writer.py `_BASE_DIR`에 `..` 추가하여 프로젝트 루트 가리키게 수정 |
| 04-18 | status_server 자동 기동 부재 | main.py가 runner만 호출, status_server는 수동 실행 필요 → 기동 안 됨 | V10.31c: main.py에 `_status_server_loop` 백그라운드 thread 추가. 크래시 시 10초 후 재기동 (무한 재시작) |
| 04-18 | `NameError: LEVERAGE not defined` | 함수 내부에서 `from v9.config import LEVERAGE`를 if/elif 분기 안에서 조건부 import. 특정 경로로 실행 시 LEVERAGE 미정의 → 워치독 에러 | V10.31c: 4개 파일(strategy_core, runner, status_writer, risk_manager, dca_engine) module-level에 LEVERAGE/calc_roi_pct 단일 import 승격. 함수 내 비-alias 중복 import 전수 제거 (local shadow 방지). AST 검증으로 잔존 0건 확인 |
| 04-20 | Phase 1 dead code 정리 | `TRIM_TRAIL_FLOOR`가 V10.31c에서 로직은 제거됐으나 config 정의 + planners 2곳 import + docs 체크리스트 잔존 (dead symbol) / trail gap 주석 "fixed 0.5" vs 실코드 `0.3` 불일치 4곳 (planners 3, hedge_engine 1) | V10.31d Phase 1: (a) `TRIM_TRAIL_FLOOR` 정의·import·doc 체크리스트 전수 제거 (import chain 검증으로 잔존 0 확인) (b) 주석 "0.5"→"0.3" 4곳 수정 |
| 04-20 | 수익성 검증 불가 구조 | trades.csv PnL은 gross (수수료 차감 전). `runner.py:1103`에서 `_rcomm` 추출까진 하나 `info["_commission"]`으로 세팅 안 하고 버림 → log_trade 호출부에 fee 도달 불가. 펀딩비는 로깅 자체 부재. 결과: 약손실 원인(수수료·펀딩 누수) 시스템적 측정 차단 | V10.31d: (a) `OrderResult.fee_usdt` 필드 추가 (b) order_router의 trades 루프에서 `fee.cost`/`info.commission` 추출 (c) runner TP1_LIMIT_FULL + strategy_core TRAIL/CLOSE/FC 경로에서 log_trade에 fee 전달 (d) TRADES_COLUMNS에 `fee_usdt` **맨 뒤** 추가 (기존 파싱 인덱스 호환성 유지) |
| 04-20 | 펀딩비 완전 블랙홀 | 8시간마다 정산되는 펀딩비가 잔고에만 반영되고 분리 로깅 없음 → 특정 심볼/시점 누수 추적 불가 | V10.31d: (a) FUNDING_COLUMNS 신설 (time/symbol/funding_usdt/funding_rate/position_amt) (b) `log_funding()` 헬퍼 (c) `_funding_fetch_loop` 백그라운드 태스크 — 1h 주기 `ex.fetchFundingHistory(None, since, 500)`. 중복 방지 last_ts_ms + csv 마지막 줄 복원. 첫 실행 48h / 이후 2h 창 |
| 04-20 | trades.csv 스키마 마이그레이션 | `fee_usdt` 컬럼 추가로 기존 18컬럼 → 신규 19컬럼. `_append_csv`는 기존 헤더 유지 → 데이터 19개/헤더 18개 불일치. 파싱 자체는 split index 방식이라 동작하지만 pandas/외부 분석 시 마지막 컬럼 누락 | V10.31d: `_migrate_log_trades_schema()` 부팅 시 1회 실행. 헤더에 `fee_usdt` 없으면 기존 파일을 `.pre_v10_31d.csv`로 rename → `_append_csv`가 자동으로 신규 19컬럼 헤더 생성. **배포 시 재시작 필수** |
| 04-20 | 대시보드 성과 지표 부재 | MDD/Sharpe/CAGR 없음 → "잘하고 있나"를 감으로만 판단 | V10.31d: `_compute_perf_metrics()` — log_balance.csv 일별 마감 잔고 추출 → MDD(peak 대비 낙폭), Sharpe(daily return std × √365, rf=0), 누적 수익률. 신뢰도 경고 n<7 "무의미"/n<30 "낮음"/n<90 "참고용". 대시보드 인사이트 탭에 카드 + 7d 수수료·펀딩 누수 카드 추가. status_server.py `renderInsight` 확장 |
| 04-20 | 주의 — Sharpe 조기 신호 오독 위험 | 현재 데이터 n=2~9일. Sharpe 계산은 되지만 통계적 유의성 거의 없음 (±100%+ 오차). 대시보드 표시값을 "잘하고 있다"의 근거로 쓰면 위험 | V10.31d: 설계상 조치 — `warning` 필드로 n 경고 노출, 대시보드에 "n=X일" 부제 병기. **운영 원칙**: 30일 미만 Sharpe로 전략 변경 판단 금지. 90일+ 누적 후에만 유의미 |
| 04-20 | 신규 진입 throttle 과도 | OPEN_DIR_COOLDOWN_SEC=10분이 시간당 방향당 6건 상한선을 강제. HF_MR 12h 부재 + 롱 풀 L4/4 포화 상태에서 숏 진입까지 차단되어 시간당 0~1건 진입. 로그 실측으로 17:06 ETH 숏 발사 이후 17:16까지 숏 전체 쿨다운 확인. "진입 더디다" 체감의 직접 원인 | V10.31d: OPEN_DIR_COOLDOWN_SEC=0. 체크 로직 3곳(planners.py:915/969 진입 차단, 1091/1270 타임스탬프 세팅) 유지 — 0이면 `now_ts < now_ts`로 즉시 통과. 변수/로직 자체 제거는 Phase 3 dead code 정리로 미룸. **고지된 리스크**: 04-18 연쇄 FC 같은 상황에서 시간당 방향당 진입 상한선이 없어져 heavy side 가속 가능. 하지만 쿨다운이 실제 방어 효과를 냈다는 [실측] 근거는 없었음 (내 이전 주장은 [직관]이었음을 인정) |
| 04-20 | 일회성 마이그레이션 성공 로그 제거 | `_migrate_log_trades_schema()`의 성공 print가 V10.31d 배포 후 영원히 트리거될 일 없음 (기존 파일은 이미 백업됐고 앞으로는 항상 `fee_usdt in existing_cols`로 early return). 부팅마다 dead 코드 경로 | V10.31d 마이너: 성공 print 삭제. 실패 print(에러 추적용) + 스킵 로그(진단용)는 유지. 앞으로 심을 로그는 "정기 반복"이냐 "일회성"이냐로 분류해 일회성은 한 번 검증 후 제거 원칙 |
| 04-20 | OPEN_DIR_COOLDOWN 완전 제거 | V10.31d에서 값=0으로 무력화했던 것을 변수/체크/세팅/save/restore 전부 삭제 (Phase 3 dead code 정리). 소스 참조 0건 확인 | V10.31d-3: planners.py에서 정의·체크 2곳·세팅 2곳·global·save_strategy_state·restore_strategy_state 전수 제거. config.py:430 옛 주석 업데이트. 기존 저장된 system_state에 `_open_dir_cd` 키 잔존해도 restore 경로가 참조 안 하므로 무해 |
| 04-20 | T3 Defense TRAIL PnL 대폭 축소 (사용자 보고) | APT T3_DEF_TRAIL: 로그 -$0.64 vs 실제 -$22 (-5.24% ROI와 일치). 원인: order_router가 `ex.fetch_my_trades(limit=5)`로 realizedPnl 추출 → FORCE_CLOSE 대량 체결이 다수 조각으로 쪼개질 때 첫 5건만 합산 → 부분값. strategy_core가 `_rpnl != 0.0`이면 무조건 사용 → 부분값이 log_trade에 기록. 실측 오염 1건 확정(APT, ratio 2.9%). 메모리의 "V10.31b trim PnL 3배 뻥튀기"(트림 방향 과장)와 별개 버그 — 이번은 FC/TRAIL 방향 축소 | V10.31d-3: (a) order_router `fetch_my_trades` limit 5→50 (b) strategy_core에 검증 로직 — `abs(_rpnl) < abs(_self_pnl) * 0.5`이면 자체계산 `_self_pnl` 사용 + `[PNL_FIX]` 로그. realizedPnl 합리적 범위일 때만 그대로 사용(수수료·펀딩 반영된 정확값). 경고: runner.py limit fill 경로(TP1/TRIM)도 같은 패턴이지만 체결 조각 수 적어 부분값 위험 낮음 → 다음 세션에서 동일 적용 예정 |
| 04-20 | V10.31d 보강 로그 | 최초 배포 후 마이그레이션 함수가 조용히 early return (파일 없음 케이스) → "정말 실행됐는가?" 불확실. `_funding_fetch_loop` 복원 실패 시에도 조용히 last_ts_ms=0으로 시작 → 중복 fetch 가능성. 첫 주기 0건일 때 로그 자체가 없어 "fetch 작동 여부" 검증 불가 | V10.31d+: (a) `_migrate_log_trades_schema` 3분기(파일없음/빈줄/이미신규) 각각 명시 로그 (b) `_funding_fetch_loop` 복원 결과 4케이스 구분(파일없음/빈파일/파싱실패/정상복원) (c) FUNDING 첫 주기 0건 case 로그 추가 (d) 모든 신규 print에 `flush=True` (stdout 버퍼링으로 systemd 캡처 누락 방지) |
| 04-20 | 심볼 실적 기반 동적 조정 (Phase 3b) | Kim 판단: 기존 베타/corr 기반 선발은 미래 예측 신호. 후행지표인 실적을 소프트 반영해 (a) 지속 손실 심볼 임시 배제 (b) 선발 tiebreaker. 과적합 리스크 명시적 수용 (사용자 "거칠게 진행, 위험 감수") | V10.31e: 신규 `v9/strategy/symbol_stats.py` — `compute_symbol_stats()`(1h 캐시), `is_symbol_cooldown()`, `get_pnl_score()`. planners.py의 MR/TREND_COMP/TREND_NOSLOT 3곳에 쿨다운 체크 주입. universe_asym_v2 선발 랭킹에 `combined_score = atr_pct × (1 + 0.2 × pnl_score)` tiebreaker. status_writer/server에 심볼별 7일 실적 카드 + 🔴 CD 배지. 전체 flag `SYMBOL_STATS_ENABLED` — 문제시 즉시 원복. **한계 고지**: (1) 샘플 부족(n<5) 중립 처리했지만 현재 log_trades.csv 1일치만 존재 → 초기 1주일간 거의 동작 안 함 (2) PnL은 fee 차감한 net 사용 (V10.31d 활용) (3) 후행지표라 국면 전환 시 오판 가능 → 90일+ 누적 후 효과 재평가 필요 |
| 04-20 | T1 DCA 전 max_roi 측정 불가 (사용자 질문 "T1 DCA한 것 중 roi 1% 찍은 비율") | `runner.py:1514` 등 5곳에서 DCA 체결 시 `max_roi_seen = 0.0`으로 리셋 → T1 시점 반등 기록 소실. 청산 시 log_trade에 남는 max_roi_seen은 "DCA 이후 구간의 max"만 추적. 따라서 실측 1주일치 데이터로 T1→T2 DCA된 51건 분석해도 "T1에서 1%+ 찍었는지" 불가 (100% max_roi=0.0~0.5% 구간에 몰려 기록된 버그) | V10.31e 측정 인프라: (1) DCA 경로 3곳(runner:1514, strategy_core:269/345)에 `max_roi_by_tier` dict로 tier별 max 보존 — 리셋 직전 `setdefault("max_roi_by_tier", {})[str(old_tier)] = old_max` (2) log_trade 호출부 3곳(runner trim/TP1_LIMIT, strategy_core FORCE_CLOSE)에서 `max_roi_by_tier["1"]` 추출해 `t1_max_roi_pre_dca` 파라미터 전달 (3) schemas.py에 컬럼 추가 (맨 뒤, index 19) (4) `_migrate_log_trades_schema` 확장 — `TRADES_COLUMNS` 전수 검사 방식으로 변경해 향후 컬럼 추가도 자동 백업/재생성. **로직 영향 제로**: 기존 리셋 타이밍·값 그대로 유지, 값만 dict에 보존. **TRIM 경로는 의도적으로 건드리지 않음** — trim은 성공 시점, 측정 관심 대상 아님. **측정 가능 시점**: 2주+ 누적 후 (n≥50 TRIM/FC 거래) 사용자 질문에 정확 답변 가능 |
| 04-20 | 429 Too Many Requests 반복 발생 | 04-20 04:08~04:17 8회 실측. 원인 분해: `fetch_tickers()` weight 40 × 메인 루프 1초 × 60회/분 = **2400** (Binance IP 한도 정확히 근접). `fetch_ohlcv` 16심볼 × 4TF × 6회/분(10s 주기) = **1920 weight/분**. 합계 4350 weight/분 → 한도 2배 초과. Universe refresh(5분) 시 33심볼 × 1h ohlcv 폭주로 순간 초과 → SYNC/snapshot 실패 연쇄. 진입 판단 자체 불가 구간 발생 | V10.31e-3: (1) `_TICKERS_CACHE` 3초 TTL 도입 → tickers 2400→800 weight/분 (2) `ohlcv_interval_sec` 기본값 10→15s → 1920→1280 weight/분. 합계 4350→2110 (한도 2400 대비 290 여유). Universe refresh 피크도 소화 가능. **trade-off**: (a) 티커 가격 최대 3초 지연 — 5m 봉 전략에 무해 (b) ohlcv 5초 지연 — 1m 지표(RSI/micro)에 약간 영향이나 체결은 5m close 기준이라 무해. 캐시 fallback 체인: `_TICKERS_CACHE["data"]` → `prev_snapshot.tickers` → 빈 dict. **재분석 결과**: `fetch_positions` 429는 30초 주기로 자체 부하 낮음, tickers 폭주에 휩쓸린 피해자 → 이번 조치로 자동 해결 [추정]. 다음 세션 검증 필요 |
| 04-20 | Falling Knife 필터가 MR 철학과 충돌 (사용자 지적) | v9.9부터 관성으로 유지된 "최근 3×5m 봉 누적 ±2% 이상이면 MR 진입 차단" 로직. MR = "이격 구간 반대 진입"인데 Falling Knife = "이격 커지면 차단" → 서로 모순. 9일 실측 분석 결과: 필터 있는 MR T3 FC 5.8% (n=52) vs 필터 없는 TREND T3 FC 4.4% (n=136). 필터가 오히려 T3 FC 비율 높이고 기회만 봉쇄 확인. 사용자 진단 "물린 구간 반대 진입이 들어와야 하는데 쟤가 막는 거네" 정확히 적중 | V10.31e-4: (1) `planners.py:843-847` 호출 제거 (2) `_is_falling_knife_long/short` 함수 정의 제거 (3) `FALLING_KNIFE_BARS/THRESHOLD` import 제거 (4) config.py 상수는 참조 없으나 롤백 포인트로 유지하며 "미사용" 주석 (5) docs/OPEN.md 조건 6번 제거 표기. **기대 효과** [추정]: MR 진입 시도 증가 → TREND 편중 완화. 실측 검증은 1주+ 누적 후 entry_type=MR 비율 확인 |
| 04-20 | **418 I'm a teapot — IP 밴 550분** (긴급) | V10.31e-3 배포 전 기존 코드로 운영 중 fetch_tickers 폭주 누적 → 429 반복 → Binance 418 DDoS 차단. 밴 지속 ~9시간 20분. 재발 시 시스템 전체 마비. 이전 V10.31e-3의 weight 2110/분도 한도 2400 대비 여유 290밖에 없어 안전마진 부족 | V10.31e-5 압력 최소 하드닝 전체 적용: (1) tickers TTL 3s→5s (분당 20→12회, weight 800→480) (2) ohlcv_interval 15s→30s (weight 1280→128 — 이전 계산 과대였음, 실제 weight 1) (3) 메인 루프 1s→2s (모든 API 빈도 절반) (4) Universe refresh 5min→15min (ohlcv 33×1h 폭주 빈도 1/3) (5) universe_asym_v2가 market_snapshot의 `_TICKERS_CACHE` 공유 (중복 fetch 제거). **실측 weight**: tickers 480 + ohlcv 128 + balance 20 + positions 10 + universe 14 + my_trades 25 = **677 weight/분** (한도 2400의 28%, 여유 72%). **trade-off**: 가격 지연 최대 5초, ohlcv 지연 최대 30초, 심볼 회전 15분 단위. 5m 봉 전략이라 진입 판단에 무해. trim/trail 반응 2초 주기로 느려짐 — 체결 타이밍 손실은 무시할 수준. **롤백 포인트**: 각 수정 지점에 V10.31e-5 주석. 1~2주 정상 운영 확인 후 약간 타이트하게 조정 가능 (tickers 4s, ohlcv 20s 등) |
| 04-20 | 418 I'm a teapot — IP 차단 발생 | 04-20 ~04:40 KST (05:10 UTC) 차단. 메시지: "banned until 1776661799909" (9시간). V10.31e-3에서 캐시 추가했지만 **사용자 서버에 미배포 상태**라 구버전 기존 폭주 그대로 진행 → 418 밴. 밴 발생 후 봇이 계속 두드리면 연장 위험. weight 계산 재점검: `fetch_tickers` weight 40×60회=2400 단독으로 한도 전부 소진 | V10.31e-5 다중 하드닝: (1) 메인 루프 1→2s (모든 API 호출 빈도 절반) (2) tickers 캐시 3→5s (3) ohlcv 15→30s (4) universe 5→15min (5) ccxt `rateLimit` 50→100ms (개별 호출 간격 보장) (6) **418 자동 감지+회복**: market_snapshot이 418 감지 시 "banned until" ms 파싱해 `/tmp/trinity_ban_until.txt` 저장 → 메인 루프가 read해서 해제까지 60초 간격 슬립 (API 호출 전혀 안 함, 밴 연장 방지). 해제 30초 전부터 플래그 삭제 후 정상 복귀. 예상 weight: ~750/분 (한도 2400의 31%). 418 재발 시 폭주→장기 밴 순환 근본 차단 |
| 04-20 | HEDGE_SIM 중간형 DCA 시뮬 확장 (사용자 요구) | 기존 HEDGE_SIM (v10.29e)은 MR T1 진입가만 기록 → 청산 시 단순 ROI 비교. 실전 MR은 DCA까지 물리는데 시뮬은 반영 안 해 **과소평가**. 사용자 요구: "트렌드 진입 시점에 MR 헷지 동시 진입 (반대 방향) + DCA까지 포함한 가격 추격 병렬 시뮬". Q1 MR 시그널 심볼 / Q2 TREND 동일 notional / Q3 독립 종료(TP1 2% / HARD_SL -10%) | V10.31e-6: (1) `schemas.py` `HEDGE_SIM_COLUMNS` 14개 추가 (2) `logger_csv.py` `log_hedge_sim()` 함수 (3) `planners.py:1187~` HEDGE_SIM 기록 시점에 시뮬 필드 확장 — tier/blended_ep/t2_notional/t3_notional/DCA 트리거 맵/TP1/HARD_SL 임계 (4) `runner.py` 신규 `_tick_hedge_sim()` 매 틱 호출 — ROI 계산, DCA 트리거 시 평단 압축, 종료 조건 도달 시 log_hedge_sim 기록 (5) 메인 루프에 `_tick_hedge_sim(system_state, snapshot)` 1줄 삽입 (6) docs/TREND.md에 설명 추가. **실전 영향 0**: 읽기 전용 + 자체 state + try/except. 시뮬 파라미터는 실전 config 동일 (`DCA_WEIGHTS=[33,33,34]`, `DCA_ENTRY_ROI_BY_TIER={2:-1.8,3:-3.6}`, `TP1_FIXED[1]=2.0%`, `HARD_SL_BY_TIER[3]=-10%`). **한계** [필수 고지]: (a) 가상 MR은 "이상적 조건" 가정 — 실제 MR 게이트(BB/RSI/VS/corr) 통과 필요해 재현 불가 (b) 샘플 쌓이는 속도 ≈ TREND_COMP 발사 빈도 (c) LEVERAGE 동일 가정, 수수료·펀딩 미반영. **집계 가능 시점**: 2주+ 후 n≥30 시뮬 종료 데이터로 "TREND_COMP 전략 vs 가상 반대진입" 비교 |
