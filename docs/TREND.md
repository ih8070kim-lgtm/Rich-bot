# TREND — 체크리스트

## 함정
- NOSLOT은 pending 구조 금지 — MR fill이 없으므로 intents.append 즉시 발사
- _noslot_best는 글로벌 최고 score 1개만. 루프 내 즉시 발사하면 다수 동시 진입
- TREND 포지션은 role=CORE_MR — 슬롯 카운팅에 포함됨 (BC/CB와 다름)
- ★ V10.30: score cap 5.0 — abs(score) > 5.0이면 과열로 판단, 진입 차단
- ★ V10.30: TREND_COOLDOWN_SEC = 0 — _open_dir_cd(10분)가 실질 제약
- ★ V10.30: trigger_side=None 시 반드시 continue (side=None crash 방지)

## 수정 시 체크
- [ ] NOSLOT이 intents.append 사용
- [ ] _noslot_best가 루프 종료 후 1건만 발사
- [ ] _open_dir_cd 쿨다운 적용
- [ ] HEDGE_SIM 기록이 정상 작동하는지
- [ ] score cap (TREND_MAX_SCORE) 체크가 양쪽 NOSLOT 탐색에 적용되는지
- [ ] trigger_side=None 블록 끝에 continue 유지
