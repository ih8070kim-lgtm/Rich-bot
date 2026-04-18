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

## 수정 시 체크
- [ ] NOSLOT이 intents.append 사용
- [ ] _noslot_best가 루프 종료 후 1건만 발사
- [ ] _open_dir_cd 쿨다운 적용
- [ ] HEDGE_SIM 기록이 정상 작동하는지
- [ ] score cap (TREND_MAX_SCORE) 체크가 양쪽 NOSLOT 탐색에 적용되는지
- [ ] trigger_side=None 블록 끝에 continue 유지
