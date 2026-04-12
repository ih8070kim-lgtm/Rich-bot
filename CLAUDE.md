# Trinity V10.29e — AI 제어 규칙

## 필수 작업 규칙
- 함수 삭제/이동 전 `grep -rn "함수명" --include="*.py"` 실행하여 외부 참조 확인
- `apply_order_results` 등 시그니처 변경 시 모든 호출부 동시 수정 (grep으로 확인)
- 수정 완료 후 `python3 -c "import ast; ast.parse(open('파일').read())"` 문법 검증
- BC/CB는 **모든** 슬롯 카운팅에서 제외 (slot_manager, planners._core_long/short, _HEDGE_ROLES_SLOT)
- DCA 처리 시 stale tp1_limit_oid/tp1_preorder_id 반드시 클리어
- trail close는 p["amt"] 전량 — 잔량 남으면 바이낸스 유령 포지션 발생
- pending 구조는 fill 이벤트 없으면 영원히 발사 안 됨 → 즉시 발사 필요 시 intents.append

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
