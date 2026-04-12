# DCA — 물타기 구조

## DCA Tier 구조
```
T1 → T2: ROI ≤ -1.8%  (DCA_ENTRY_ROI_BY_TIER)
T2 → T3: ROI ≤ -3.6%
T3 이후: DCA 없음 (T4 제거됨)
```

## DCA 비중 (DCA_WEIGHTS = [25, 25, 50])
```
T1: 25% (스캘핑)
T2: 25% (버퍼)
T3: 50% (스윙)
합계: 100% = grid_notional = equity/8 × 3
```
예: equity=$3,500 → grid=$1,312 → T1=$328, T2=$328, T3=$656

## DCA 처리 시 필수 클리어 (strategy_core.py)
```python
# DCA 체결 후 반드시:
p.pop("tp1_limit_oid", None)      # ★ stale TP1 limit 제거
p.pop("tp1_preorder_id", None)    # stale preorder ID 제거
p.pop("tp1_done", None)           # TP1 완료 플래그 리셋
p["step"] = 0                     # trail step 리셋
p["trailing_on_time"] = None      # trail 시간 리셋
p["max_roi_seen"] = 0.0           # max ROI 리셋
p["worst_roi"] = 0.0              # worst ROI 리셋
# stale limit 바이낸스 취소큐 추가
_TRIM_CANCEL_QUEUE.append({"sym": sym, "oid": stale_oid})
```

### 버그 사례
- **2026-04-12**: DCA 시 tp1_limit_oid 미클리어
  → T1에서 TP1 limit 걸림 → T2/T3 DCA → stale limit이 trim 영구 차단
  → LINK +1.4%인데 trim 안 됨

## EP 계산 (블렌디드)
```
total_cost = (기존_amt × 기존_ep) + (신규_amt × 체결가)
new_ep = total_cost / new_amt
```
DCA_ENTRY_BASED = False → 블렌디드 EP 기준 (바이낸스 ROI = 봇 ROI)

## DCA 쿨다운
```
DCA_COOLDOWN_BY_TIER = {2: 0, 3: 0, 4: 0}  # 전면 제거
```

## DCA 차단 조건
- CORR_GUARD 발동 중
- Killswitch MR ≥ 0.85
- role이 HEDGE/SOFT_HEDGE/INSURANCE_SH/BC/CB
- pending_dca 이미 설정됨

## locked_regime
DCA 시 regime을 기록하여 이후 DCA에서 wider regime 적용.
`_wider_regime()` 함수 — planners.py에 존재 필수 (strategy_core.py에서 import).

## 수정 시 체크
- [ ] tp1_limit_oid 클리어 코드 유지되는지
- [ ] _wider_regime 함수가 planners.py에 존재하는지
- [ ] ep 계산이 블렌디드 방식 유지되는지
- [ ] DCA 처리 후 max_roi/worst_roi 리셋되는지
