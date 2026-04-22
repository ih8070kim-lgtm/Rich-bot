# OPEN — 신규 진입 (MR)

## 진입 조건 (AND)
```
1. 5m RSI ≤ OS(과매도) 또는 ≥ OB(과매수)  — ATR 연동 동적 임계값
2. 5m ATR boost 트리거 (가격 ≥ EMA10 ± ATR×배수)
3. micro RSI 확인 (15m RSI 일치 방향)
4. VS (Volume Surge) ≥ 1.0 — 최근 5봉/30봉 거래량 비율
5. 상관계수 ≥ OPEN_CORR_MIN (0.60)
6. ★ V10.31e-4 제거: Falling Knife 필터 (9일 실측 효과 없음)
7. 방향별 쿨다운: ★ V10.31d 제거 (OPEN_DIR_COOLDOWN_SEC=0)
8. 심볼별 open_fail_cooldown 통과
```

## 포지션 사이징
```
grid_notional = equity / GRID_DIVISOR(8) × LEVERAGE(3)
T1_notional = grid_notional × DCA_WEIGHTS[0] / sum(DCA_WEIGHTS)
            = grid_notional × 25/100 = 25%
qty = T1_notional / price
```
예: equity=$3,500 → grid=$1,312 → T1=$328 → ETH@$2300 → 0.142개

## Entry Type
```
MR        — EMA10 기반 평균회귀
15mE30    — EMA30 기반 (MR 미충족 시 보조, MAX_E30_SLOTS=2)
TREND     — TREND_COMP 시그널 기반 (MR 슬롯 여유 시)
TREND_NOSLOT — MR 슬롯 풀 시 즉시 발사
COUNTER   — BB Squeeze 브레이크아웃 (dca_engine.py)
```

## TREND_COMP vs TREND_NOSLOT
```
MR 슬롯 여유 있음:
  MR 시그널 → _pending_trend_comp 저장 → MR fill 후 발사 → TREND_COMP

  ★ V10.31i: 스큐 예방 가드 — COMP는 스큐 예방 목적 (주 수입원 아님)
    `_tr_opp_slots < _sig_side_slots` 충족 필요
    수식 도출: 발사 후 opp는 +1, sig도 MR로 +1 → (opp+1) < (sig+1) = opp < sig
    의미: 발사 후에도 COMP 방향이 MR 방향보다 최소 하나 적게 유지
    예시 (발사 전 롱/숏):
      3/1 buy 시그널 → opp=숏=1, sig=롱=3 → 1<3 ✓ 발사 (4/2, 스큐 완화)
      3/2 buy 시그널 → opp=숏=2, sig=롱=3 → 2<3 ✓ 발사 (4/3)
      2/2 buy 시그널 → opp=숏=2, sig=롱=2 → 2<2 ✗ 차단 (신규 스큐는 MR 전담)
      1/3 buy 시그널 → opp=숏=3, sig=롱=1 → 3<1 ✗ 차단 (역방향 가속)
      1/1 buy 시그널 → opp=숏=1, sig=롱=1 → 1<1 ✗ 차단
    근거: COMP는 MR과 동시 발사라 양쪽 +1 동시 → |sig-opp| 불변.
          NOSLOT A 조건과 달리 "비대칭 해소" 효과 없고, 오직 "사전 존재 스큐에
          대한 반대편 보강" 역할. 균형/opp우세 상태에서 발사는 의미 없음.
    위반 시 5분 1회 [COMP_SKIP_SKEW] log_system 기록 (조건별 키)

MR 슬롯 풀:
  MR 시그널 → 전체 심볼 score 스캔 → 최고 1개 즉시 발사 → TREND_NOSLOT
  쿨다운: ★ V10.31d 제거 — 과거 _open_dir_cd 10분은 시간당 방향당 6건 상한선을 만들어 진입 지연 주원인이었음
  제한: 틱당 1개 (상대평가 1위만)

  ★ V10.31h: A 조건 — 발사 후에도 비대칭 유지될 때만 발사
    `(_tr_opp_slots + 1) < _sig_side_slots` 충족 필요
    예시:
      롱4 + 숏3 → NOSLOT 숏 발사 후 4+1=4, sig=4 → 균형 도달 → ★ 차단
      롱4 + 숏2 → NOSLOT 숏 발사 후 2+1=3 < 4 → 비대칭 유지 → ✓ 발사
      롱4 + 숏4 → 4+1=5 > 4 역전 → ★ 차단
    근거: 04/20 14~15시 NOSLOT 13건 다발 패턴(TIA 시그널 5건 연쇄)이 비대칭 해소 후
          누적 발사로 양쪽 슬롯 다 채우는 양산 메커니즘 차단.
    위반 시 5분 1회 [NOSLOT_SKIP_A] log_system 기록 (조건별 키)
```

### TREND score 계산
```
score = EMA이격(ATR단위) × 거래량서지(5봉/30봉) × (1 + |RSI극단|)
양수 = 상승추세, 음수 = 하락추세
자격기준: |score| > 0.5 (후보 풀 진입)
선택기준: 상대평가 1위
```

## 주문 방식
```
MR/E30/COUNTER → limit (OPEN_WAIT_NEXT_BAR=False면 market)
TREND          → market (즉시 체결)
BC/CB          → market
```

## 수정 시 체크
- [ ] _core_long/_core_short 카운팅에 새 role 제외 추가했는지
- [ ] can_long/can_short 조건에 영향 없는지
- [ ] TREND_NOSLOT에서 intents.append 사용했는지 (pending 아님)

## V10.31v: OPEN PARTIAL 80% 미만 즉시 청산

- 근거: limit 부분체결 = 시장이 entry 방향 반대로 움직이는 중 = 이미 불리한 시장 신호
- 작은 사이즈로 슬롯 묶이는 것보다 다음 기회 노림이 합리적
- 구현: `runner._manage_pending_limits` 5분 타임아웃 후 체결률 계산
  - `_fill_ratio = part_filled / info["qty"]`
  - OPEN intent & < 0.80 → 시장가 역방향 청산 (positionSide 유지)
  - DCA는 기존대로 부분체결도 기존 포지션에 합산 (평단 조정 목적이라 OK)
- 실측 04-21~22: PARTIAL 2/49건 (4.1%)
  - SUI 67% 체결 $277 (유지됨)
  - XRP 12% 체결 $48 (V10.31v 이후 자동 정리 대상)
- 로그: `OPEN_PARTIAL_CLEAR` 태그 (log_system.csv) — 정리된 심볼/비율/qty 기록
- fallback: 시장가 청산 실패 시 기존 동작 (부분체결분 포지션 등록)
