# BC — 체크리스트

## ★ V10.31AM3: 진입 진정 검증 3중 AND (사용자 컨셉 [04-26])

사용자 컨셉: "peak 후 거래량 식음 → 계단식 하락 시작 → 그 계단에 합류"

기존 BC ENTRY는 ARM tf의 1h excess가 baseline으로 회귀했는지만 확인 → "베타가 식었나"만 보고 "거래량/모멘텀이 식었나"는 안 봄. peak 직후 모멘텀 살아있는 상태에서 진입 → 아직 식지 않은 하락에 휘말리는 패턴 관찰됨 (NOT BC peak +18.6% 직후 진입 케이스). 3중 AND 가드 추가:

```
1. 거래량 median 대비 ≤ 1.5x  (★ V10.31AM3 hotfix-2: mean → median)
2. 거래량 peak 대비 ≤ 70%
3. RSI 1h < 60 + 하락 방향 (RSI[-1] < RSI[-2])
```

### baseline 산정 (mean → median)

**기존 mean의 결함**: 7일=168시간 윈도우에 ARM 시점 peak 거래량(평균 대비 600%급)이 자기포함되어 baseline 자체가 부풀려짐. 임계 1.5x는 사실상 mean 1.0x 수준의 약한 필터로 작동. 코드 주석 "1.2→1.5x 완화 (peak 포함 보정)"은 임시 보정.

**V10.31AM3 hotfix-2**: `np.median(_vols[-168:])` 도입. peak outlier 영향 거의 없는 견고한 추정량 → "정상 베이스라인" 정의에 정합. 임계 1.5x 유지 시 median 기준으론 **실효성 강화**.

```python
# beta_cycle.py:305
_v_baseline = float(np.median(_vols[-168:])) if len(_vols) >= 1 else 0.0
if _v_recent / _v_baseline > 1.5:  # 임계 mean 시절 그대로
    continue
```

### NORM_THRESH tf 정합

**ARM tf 기준 NORM 체크**:
- 1h ARM → 1h excess ≤ baseline
- 1d ARM → 1d excess ≤ baseline (이전엔 1h만 봐서 1d ARM 진입 못 함 버그)

ARM 시 `peak_vol` 저장 (peak 비교용 기준점, 7일 median과 별개로 peak 자체와의 비율도 검증).

### 진입 빈도 모니터링

`[직관]` median 도입은 진입 빈도 감소 방향 — AM3의 BC_PULLBACK_MAX 5→8(윈도우 확대)와 일부 충돌. 1주 모니터 후 진입 부족 시 임계 1.5 → 1.8~2.0 완화 검토.

검증 가능 로그: `[BC] ⏭ SKIP {sym} 거래량 미진정 (median 대비 X.Xx > 1.5x)` 빈도 추적.

---

## ★ V10.31AM: BC/CB 노셔널 차감 제거 (MR 사이즈 복원)

**Before (V10.31b~AL)**: `_mr_available_balance()`가 BC/CB 포지션 노셔널만큼 real_balance에서 차감 (최소 30% 보장). TREND 활성 시 슬롯/마진 충돌 방지 목적.

**After (V10.31AM)**: TREND off 상태 (V10.31AD 이후)라 마진 여유 충분. 함수 내부만 `return real_balance_usdt` (전체 반환)으로 단순화. 호출부 6곳 시그니처 유지.

**안전장치**: KILLSWITCH (margin_ratio 기반 80/85/90% 3단계)가 통합 관리. BC x1 + MR x3 혼재 상태도 Binance margin_ratio에 통합 반영됨.

**기대 효과**: BC $400 활성 시 MR T3 노셔널 $1121→$1271 (+13.4%).

**재활성 방법**: `planners.py _mr_available_balance()` 내부 주석 블록 복원.

---

## 함정
- 상방 베타만 사용 (BTC 상승봉). 전체 베타 쓰면 하락 반등도 잡아서 손실
- BC/CB는 모든 슬롯 카운팅에서 제외 — SLOTS.md 5곳 확인
- order_router에서 lev=1 적용. runner.py 레버리지 초기화 후 BC 심볼 x1 복원 필요
- MR HARD_SL이 BC 포지션 kill하는 버그 있었음 — BC role 체크로 분리됨
- ★ V10.30: 고정 NORM_THRESH 제거 → baseline 적응형 진입
- ★ V10.30: REHEAT 즉사 방지 — 진입 시 excess가 baseline으로 복귀해야 함

## 진입 흐름 (V10.30)
```
ARM: excess ≥ 5% (BC_ARM_THRESH)
  → baseline = ARM 직전 72h excess 중앙값 기록
  → peak_excess, peak_price 기록

ENTRY: 1h excess ≤ baseline ("진짜 식었다")
  + pullback 0.5~8%
  + 볼륨 확인

REHEAT: 1h excess ≥ 5% → 손절 (thesis 실패)
  → baseline 복귀 후 진입이므로 즉사 구조적 불가
```

## 수정 시 체크
- [ ] _HEDGE_ROLES_SLOT에 "BC" 포함
- [ ] order_router lev=1
- [ ] runner 레버리지 복원 시 _bc_cb_role_map 참조
- [ ] HARD_SL/ZOMBIE에서 BC role 제외
- [ ] 상방 베타 계산 (_up_mask) 유지
- [ ] _calc_baseline_excess가 72h 구간 계산하는지
- [ ] ARM 시 baseline이 _armed dict에 저장되는지
- [ ] 진입 시 bc_baseline이 position metadata에 저장되는지

## ★ V10.31c 변경
- **TRAIL에서 wick(h_1h) 제거** — `price >= trail_stop`만 체크. SL은 wick 유지(손실 방어).
  - 변경 전: 1시간봉 고가가 trail_stop 넘으면 즉시 청산 → 순간 스파이크로 과민반응
  - 변경 후: 현재가가 trail_stop 넘을 때만 청산 → 노이즈 감소
- **peak_roi / giveback shadow logging 추가** (`BC_EXIT` 이벤트)
  - `p["bc_peak_roi"]`: 매 틱 갱신되는 최고 ROI
  - 청산 시: `exit=±x% peak=+y% giveback=+z% hold=nh`
  - 2~3주 데이터 수집 후 activation 3%/floor 1.5% 재검토

## ★ V10.31AF: 리포트 집계에서 BC 제외
- **일일 리포트 총 PnL/WR/avg/tier/reason/trim 전부 BC 제외** — `core_trades` 리스트 분리
- **7일 히스토리 (`_load_daily_pnl`)에서도 BC 제외** — `role == "BC"` 행 스킵
- **대시보드 (`_load_trade_stats`)에서도 BC 제외** — 코어 전략 성과에 BC 섞이지 않음
- **Role 섹션(🎭)에만 BC 별도 라벨 표시** — 참고용으로 건수/PnL 확인 가능
- **CB도 동일 제외** — CB도 별도 전략(x1 레버리지)이라 같이 분리
- 영향 파일: `telegram_engine.py` 3곳 (`_load_daily_pnl`, `_load_trade_stats`, `generate_daily_report`)
- 시뮬 PASS: core 5건 + BC 3건 + CB 2건 → 총 PnL $+11.00 (BC/CB 제외) / 🎭 섹션에 MR/BC/CB/Hedge 모두 표시 ✓
- 주의: `log_trades.csv` 자체는 변경 없음 (BC 거래도 기록 유지) — 리포트 **표시**만 분리

## ★ V10.31AI: BC/CB x1 ROI 일관 반영
- **버그**: `log_trades.csv` roi_pct 컬럼과 `status_writer.py` 대시보드에서 BC/CB ROI가 x3 계산되고 있었음 — 실제 체결은 x1인데 표시만 x3이라 3배 뻥튀기
- **실증 [실측]**: DYM BC 실거래 기록 `roi=-27.785%`, 실제 가격 변화 `-9.262%` (정확히 3배 차이)
- **helper 함수 도입** (`v9/utils/utils_math.py`):
  - `role_leverage(role)` → BC/CB=1, 나머지=LEVERAGE(3)
  - `calc_roi_pct_by_role(ep, cp, side, role)` → role 기반 자동 적용
- **5곳 수정**: `strategy_core.py` L422, L580 / `runner.py` L1720, L1773, L1823 / `status_writer.py` L276 — BC/CB role이 실제 거치는 경로만 선별
- **이미 올바른 곳**: `logger_ml.py:286`, `telegram_engine.py:201,357` (V10.29d~e부터 처리됨)
- **영향 없음**: MR/HEDGE 경로 30+ 곳은 기존 `LEVERAGE` 그대로 유지 — BC/CB 도달 불가
- **과거 오염 데이터**: 수정 불가(이미 기록됨). V10.31AF BC/CB 제외 필터로 리포트 노출은 차단됨. 신규 청산부터 정확 기록
