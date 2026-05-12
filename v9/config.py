"""
V9 Trinity Config  (v10.28 — 2026-04-05 MR 전략 복원 패치)
===============================================
v10.27f → v10.28 변경:
  Heavy DCA 차단      urgency 기반 heavy side DCA 차단 전면 제거
  독립 DCA Trim       gate 조건 제거 → tier entry +2% ROI 달성 시 무조건 trim
  (진입 ATR 패널티 / TP 할인 / light block은 유지)
"""

VERSION = "14.16"  # ★ V14.16 [05-12]: BTC 1h ±0.5% 필터 + MR 부활(BTC 방향 일치 시) + trail 0.7/0.4

# ═══════════════════════════════════════════════════════════════════
# ★ V10.31AA: Feature Flags — MR + PTP 모드 (단순화 실험)
# ═══════════════════════════════════════════════════════════════════
# 근거 (실측 4일 241건 분석):
#   MR 단일:             +$20.99 (99건, 승률 91.9%, EV +$0.21/건)
#   MR + TREND + HEDGE:  -$102.77 (241건, TREND EV -$0.84/건)
#   PTP 어떤 세팅도 조합을 구원 못함 (최선 -$53)
#
# 전략: MR 본연 + PTP(portfolio drop trail)만, TREND/HEDGE 비활성
# 롤백: 이 값들만 True로 변경하면 즉시 원복 (코드 보존)
#
# 미래 개선 계획 (1개월 데이터 누적 후):
#   - OP/SOL/AVAX/DOT/FIL/PENDLE (추세 지속형)만 TREND whitelist
#   - 나머지는 MR 전용
#   - HEDGE는 HIGH 레짐 한정

TREND_NOSLOT_ENABLED = True  # ★ V14.2 [05-06]: 활성화 — 사용자 결정. 메모리 [실측 V10.31AA EV 음수 증명] 인지하고 재시도. MR 시그널 없을 때 추세 단독 진입 (1대1 매칭이 메우지 못하는 슬롯 보충)
HEDGE_COMP_ENABLED   = False  # ★ V14.6 [05-06]: TREND_COMP OFF — 사용자 결정 "처참, NOSLOT만 유지". 메모리 [실측 V10.31u -$30, ARB -$50] + V14.5 상대평가 변경 후에도 손실 누적
# 메모리 [실측 04-24]: HEDGE_COMP off 결정 — MR 메인 HARD_SL -29.23 vs 헷지 trim +0.63 패턴
# V13 [05-06]: 사용자 재활성 결정 + 사이즈 MR 풀사이즈 (T1+T2+T3 합산)로 변경
# 위험: 손실 임팩트 V11 hf 시기 대비 3배 (MR T1 33% → 풀사이즈 100%)
# BC (Beta Cycle) / CB (Circuit Breaker)는 별도 독립 전략 — 그대로 유지

# ═══════════════════════════════════════════════════════════════════
# 슬롯 설정
# ═══════════════════════════════════════════════════════════════════
TOTAL_MAX_SLOTS = 8    # ★ V10.31c: 5/5 → 4/4 단순화 (실운영은 4/4였음, HEDGE 합산용 여유 제거)
MAX_LONG        = 4    # ★ V10.31c: 5→4
MAX_SHORT       = 4    # ★ V10.31c: 5→4
MAX_MR_PER_SIDE = 4    # ★ v10.15: MR은 방향당 최대 4
MAX_E30_SLOTS   = 2    # ★ V10.27d: EMA30 A/B 테스트 슬롯 (롱+숏 합산)
MAX_HEDGE_SLOTS = 1    # ★ 최종 아키텍처: tail-risk 보험 1개만
GRID_DIVISOR    = 8    # ★ v10.15: 포지션 사이징 분모 (슬롯 상한과 독립)

DYNAMIC_SLOT_INITIAL          = 2
DYNAMIC_SLOT_EXPAND_3_TRIGGER = 2   # 한쪽 T2 발생 → 반대 방향 슬롯 1개 확보
DYNAMIC_SLOT_EXPAND_4_TRIGGER = 3   # T3 발생 → 반대 방향 슬롯 2개 확보
DYNAMIC_SLOT_EXPAND_4_ALT     = 2   # T2 2개 이상 → 반대 방향 슬롯 2개 확보

# ═══════════════════════════════════════════════════════════════════
# 레버리지 / 수수료
# ═══════════════════════════════════════════════════════════════════
LEVERAGE  = 3
FEE_RATE  = 0.0002   # ★ v10.9: 바이낸스 VIP0 maker 0.02%

# ═══════════════════════════════════════════════════════════════════
# Kill-Switch
# ═══════════════════════════════════════════════════════════════════
KILLSWITCH_FREEZE_ALL_MR = 0.9   # MR≥0.9 → 동결 (청산 계열만 허용)
KILLSWITCH_BLOCK_ALL_MR  = 0.85  # ★ V10.29d: 0.8→0.85 OPEN/DCA 금지
KILLSWITCH_BLOCK_NEW_MR  = 0.80  # ★ V10.29d: 0.7→0.80 OPEN 금지 (T3 다수 보유 시 여유 확보)

# ═══════════════════════════════════════════════════════════════════
# HARD_SL
#   ATR factor = max(FACTOR_MIN, min(FACTOR_MAX, atr_mult))
#   범위: -13.0% ~ -14.95% (≈ 13~15%)
# ═══════════════════════════════════════════════════════════════════
HARD_SL_BASE        = -6.5   # ★ v10.6: planners.py _hard_sl_base 와 일치 (-6.5%)
HARD_SL_FACTOR_MIN  =  0.85  # ATR 작음 → -5.5% (6.5 * 0.85)
HARD_SL_FACTOR_MAX  =  1.15  # ATR 큼   → -7.5% (6.5 * 1.15)
HARD_SL_ATR_BASE    =  0.0015

# ═══════════════════════════════════════════════════════════════════
# DD 셧다운
# ═══════════════════════════════════════════════════════════════════
DD_SHUTDOWN_THRESHOLD = -0.07   # ★ 최종 아키텍처: 3%→7% (스윙형 DCA -8.25% 간격 맞춤)
DD_SHUTDOWN_HOURS     = 12

# ═══════════════════════════════════════════════════════════════════
# T4 최대 손실 제한
# ═══════════════════════════════════════════════════════════════════
T4_MAX_LOSS_PCT = 0.07

# ═══════════════════════════════════════════════════════════════════
# ★ V10.29d: DCA 거리 — 레짐 차등 제거, 전 레짐 통일
# ═══════════════════════════════════════════════════════════════════
DCA_DISTANCES = {
    "LOW":       [0.010, 0.008, 0.018],
    "MID":       [0.010, 0.008, 0.018],
    "HIGH_UP":   [0.010, 0.008, 0.018],
    "HIGH_DOWN": [0.010, 0.008, 0.018],
    "HIGH":      [0.010, 0.008, 0.018],
    "NORMAL":    [0.010, 0.008, 0.018],
}

# ═══════════════════════════════════════════════════════════════════
# DCA 비중 — ★ V10.22: 4단 [15,20,30,35]
#   T1=scout(15%) → T2=확인(20%) → T3=본격물타기(30%) → T4=최종구제(35%)
#   누적: 15/35/65/100  |  T3에서 이미 65% → 강한 단가 압축 + T4 탄약 유지
# ═══════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════
# DCA 비중 — ★ V10.31AO: 스켈핑 패러다임 [33, 67] T3 제거
#   사용자 결정 [04-30]: "T3 급행열차" → T3 자체 제거
#   T1=33% scout, T2=67% commit (떨어지면 큰 사이즈로 평단 압축)
#   MR 시스템 본질 정합 (회귀 가정 → 작게 진입, 떨어지면 큰 사이즈)
#   비중 시뮬 [실측 19일]: 33/67이 PnL 1위 ($1,712), 67/33은 $1,250 (37% 차이)
# ═══════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════
# ★ V11 [05-04]: 단순 봇 — 사용자 마지막 결정
#   "어 마지막이다 이걸로 해보자"
# 
# 변경:
#   1. DCA 제거: T1만 100% 사이즈 진입
#   2. SL: -0.8% (절충 — 손익비 1:1.9, slippage/spike 위험 ↓)
#   3. TP: +1.5%
#   4. PTP: 비활성 (V10.31AO-hf15 유지)
# 
# [실측] 5일 105건 시뮬 근거:
#   현재 봇 (DCA + 사다리): -$31 / 5일
#   V11 (-0.7/+1.5): +$495 시뮬, 그러나 여유 8%p 좁음, slippage 위험 ↑
#   V11 (-0.8/+1.5): +$434 시뮬, 여유 12%p (sweet spot)
#   V11 (-1.0/+1.5): +$344 시뮬, 여유 11%p (안전)
#   
# 손익비 1:1.9, 손익분기 WR 35%
# 실측 WR [추정] 47% > 35% = 12%p 여유 (안정 흑자)
# 
# 위험 요소:
#   - 5일 over-fitting 위험 (한 달 평균 다를 수)
#   - slippage [추정 -$30/월]
#   - spike 잘못 cut [추정 5~10% 빈도]
# ═══════════════════════════════════════════════════════════════════
DCA_WEIGHTS = [33, 33, 34]  # ★ V13 [05-06]: MR 3단 DCA 부활 (T1=33%, T2=33%, T3=34%)

DCA_LIMIT_TIMEOUT_SEC = 60
DCA_MIN_CORR          = 0.5

# ★ V10.28b: Entry 기준 DCA — 이전 tier 체결가에서 X% 하락 시 트리거
# EP 기준이 아닌 실제 가격 간격 유지 (EP 압축 방지)
DCA_ENTRY_BASED   = False  # ★ V10.29b: 블렌디드 EP 기준 통일 (바이낸스 ROI = 봇 ROI)
DCA_ENTRY_ROI     = -1.5   # 레거시 호환 (T2 기본값)
# ★ V10.31AO [04-30]: T2 DCA -1.0%, T3 제거 (스켈핑)
#   사용자 결정: 빠른 평단 압축 + T3 제거로 깊은 손실 차단
#   MR 회귀 가정 → 가까이서 평단 압축 → 작은 반등에도 회복 가능
#   T3 제거 + T2 디펜스 사다리(-1.5/-2.0/-2.5/-3.0)로 큰 손실 cap
DCA_ENTRY_ROI_BY_TIER = {2: -1.8, 3: -3.0}  # ★ V14.2 [05-06]: T3 trigger -3.6 → -3.0 (사용자 결정)

# ★ V10.31AO [04-30]: T2 다단계 디펜스/SL 사다리 (T3 제거된 스켈핑 패러다임)
#   사용자 결정 [04-30]: T3 제거 + T2 단계에서 사다리식 보호
#   컨셉: T2 진입 후 worst 깊이 비례 빠른 탈출 — 깊이 갈수록 회복 기대치 ↓
#
#   사다리 (worst_enter, exit_roi, mode):
#     -1.5% → +0.5%  TRIM (T2 사이즈 부분 청산, 회복 시 T1로 복귀)
#     -2.0% → -0.5%  SL (회복 cut, 약손실 탈출)
#     -2.5% → -1.5%  SL (회복 cut, 중간 손실)
#     -3.0% → -2.5%  SL (회복 cut, 깊은 손실 — V10.31AO-hf9 추가)
#     -3.5%          HARD_SL (즉시 전량 컷, 무한 보유 차단)
#
#   변경 의도 (사용자):
#     - T3 제거로 단순화 (T1+T2만 운영)
#     - T2 단계에서 사다리로 단계적 보호
#     - 마지막 -3.5 HARD_SL은 안전망 (회복 못 한 케이스 무한 보유 차단)
#     - V10.31AO-hf9 [05-02]: -3.0 SL 단계 추가 (회복 기회 보존, HARD_SL 임계 -3.0→-3.5)
#
#   PTP 차단 정책: _ptp_active_syms 활성 시 본 로직 차단 (PTP 우선)
# ═══════════════════════════════════════════════════════════════════
# ★ V11 [05-05]: T1 단일 진입 사다리 (사용자 결정)
#   "구조상 무조건 슬리피지가 뜨네 차라리 사다리가 낫겠다"
#   "−0.7 도달 시 −0.4, −0.9 도달 시 −0.7, −1 도달 시 SL"
#
#   [실측 5일] V11 단순 SL -0.8% 한 방 cut → slippage -0.34%
#   사다리 의도: 회복 시 작은 손실로 cut, 깊이 가면 HARD_SL
#
#   동작:
#     worst ≤ -0.7% 도달 → max(roi)가 -0.4% 회복 시 전량 cut (limit 가능)
#     worst ≤ -0.9% 도달 → max(roi)가 -0.7% 회복 시 전량 cut
#     worst ≤ -1.0% 도달 → HARD_SL 즉시 전량 cut (market)
#
#   장점:
#     -0.7% 갔다가 회복 시 -0.4%만 잃음 (단순 SL은 -0.8%)
#     -0.9% 갔다가 회복 시 -0.7%만 잃음 (단순 SL은 -0.8%, slippage -1.13%)
#     -1.0% 도달 = 추세 가속 → 즉시 cut
#
#   단점:
#     코드 추가 (V10.31AO 사다리 부활)
#     일부 케이스 단순 SL 대비 손익비 분산
# ★ V14 [05-06]: 사다리를 dca_level별로 분리
#   - 사다리1 (HEDGE_COMP/TREND_COMP T1 dca_level=1): -1.0/-1.2/-1.4
#     T1_HEDGE_LADDER로 명명 (논리 명확화)
#   - 사다리2 (MR T2 dca_level=2): -2.5%에 trim 0.5% (사용자 V14 사양)
#   - 사다리3 (MR T3 dca_level=3): T3_DEFENSE_LADDER (별도 함수)
T1_HEDGE_LADDER = [
    # ★ V14.7 [05-06]: 0.5 뒤로 — NOSLOT만 영향 (TREND_COMP V14.6 OFF)
    #   사용자 결정: 비중 50% 변경과 패키지로 SL 윈도 ↑
    #   plan_t2_defense_v2 분기: role=CORE_MR_HEDGE + dca_level=1 (NOSLOT)
    (-1.5, -1.0, "SL"),
    (-1.7, -1.4, "SL"),
    (-1.9, None, "HARD_SL"),
]

T2_DEFENSE_LADDER = [
    # ★ V14.2 [05-06]: MR T2 trim 사다리 worst -2.5 → -2.0 (사용자 결정)
    # worst -2.0% 도달 후 ROI 회복 +0.5% 시 TRIM (T2 사이즈만 부분 청산, T2→T1 복귀)
    # plan_t2_defense_v2 분기: role=CORE_MR + dca_level=2만 처리
    (-2.0, 0.5, "TRIM"),
]

# ★ V13 [05-06]: T3 사다리 활성화 (사용자 사양 4단)
#   동작 (T3 진입 후 worst 추적):
#     worst ≤ -4.0% + max ≥ -2.5% 회복 시 → -2.5% market cut
#     worst ≤ -4.5% + max ≥ -3.0% 회복 시 → -3.0% market cut
#     worst ≤ -5.0% + max ≥ -4.0% 회복 시 → -4.0% market cut
#     worst ≤ -5.5% → HARD_SL 즉시 market cut
T3_DEFENSE_LADDER = [
    (-4.0, -2.5, "SL"),
    (-4.5, -3.0, "SL"),
    (-5.0, -4.0, "SL"),
    (-5.5, None, "HARD_SL"),
]

def calc_t1_hedge_action(worst_roi: float, max_roi: float):
    """★ V14 [05-06]: HEDGE_COMP/TREND_COMP T1 사다리 액션 결정 (T1_HEDGE_LADDER 매칭).
    
    plan_t2_defense_v2의 V13 분기에서 role=CORE_MR_HEDGE일 때 호출.
    """
    matched = None
    for w_enter, exit_r, mode in T1_HEDGE_LADDER:
        if worst_roi <= w_enter:
            matched = (w_enter, exit_r, mode)
        else:
            break
    if matched is None:
        return None
    w_enter, exit_r, mode = matched
    if mode == "HARD_SL":
        return ("HARD_SL", None)
    return (mode, exit_r)


def calc_t2_defense_action(worst_roi: float, max_roi: float):
    """★ V10.31AO: T2 다단계 디펜스 액션 결정.

    Args:
        worst_roi: 현재 tier(T2) 진입 후 최저 ROI (음수, 블렌디드 EP 기준 lev 3)
        max_roi: 현재 tier 진입 후 최대 ROI

    Returns:
        (mode, exit_roi) or None
        - mode='HARD_SL': 즉시 전량 컷 (worst <= -3.0%)
        - mode='SL': max_roi >= exit_roi 도달 시 전량 컷
        - mode='TRIM': max_roi >= exit_roi 도달 시 T2 사이즈 부분 청산
        - None: 사다리 미진입 또는 임계 미도달
    """
    matched = None
    for w_enter, exit_r, mode in T2_DEFENSE_LADDER:
        if worst_roi <= w_enter:
            matched = (w_enter, exit_r, mode)
        else:
            break
    if matched is None:
        return None
    w_enter, exit_r, mode = matched
    if mode == "HARD_SL":
        return ("HARD_SL", None)  # 즉시 컷
    return (mode, exit_r)

def calc_t3_defense_action(worst_roi: float, max_roi: float):
    """★ V13 [05-06]: T3 다단계 디펜스 액션 결정.

    Args:
        worst_roi: T3 진입 후 최저 ROI (음수, 블렌디드 EP 기준 lev 3)
        max_roi: T3 진입 후 최대 ROI

    Returns:
        (mode, exit_roi) or None
        - mode='HARD_SL': 즉시 전량 컷 (worst <= -5.5%)
        - mode='SL': max_roi >= exit_roi 도달 시 전량 컷
        - None: 사다리 미진입 또는 임계 미도달
    """
    matched = None
    for w_enter, exit_r, mode in T3_DEFENSE_LADDER:
        if worst_roi <= w_enter:
            matched = (w_enter, exit_r, mode)
        else:
            break
    if matched is None:
        return None
    w_enter, exit_r, mode = matched
    if mode == "HARD_SL":
        return ("HARD_SL", None)
    return (mode, exit_r)

# ★ V10.29b: Trim — 블렌디드 EP 기준 실제 ROI로 통일
# 계단식 익절: T1 TP(+1.5%) → T2 trim(+1.0%) → T3 trim(+0.5%) — 압축 깊을수록 빠른 회수
TRIM_BLENDED_ROI_BY_TIER = {2: 1.0, 3: 0.5}  # ★ V13 [05-06]: T2 trim +1.0%, T3 trim +0.5% (33/33/34 시기 사양 부활)
# ★ V10.31d: TRIM_TRAIL_FLOOR 제거 — V10.31c에서 FLOOR 로직 이미 제거됐으나 상수/import 잔존했던 것 정리

# ★ V10.31j: Tier별 디펜스 모드 (worst_roi 기반 동적 TRIM 임계)
# T2 worst ≤ -2.0 → TRIM 임계 1.5 → 0.5 (약반등 포획)
# T3 worst ≤ -5.0 → TRIM 임계 0.5 → -0.5 (약손실 탈출)
# DCA 체결 시 worst_roi=0 리셋되므로 tier별 독립 추적
T2_DEF_WORST_ENTER     = -2.0
T2_DEF_TRIM_THRESH     = 0.5
T3_DEF_M5_WORST_ENTER  = -5.0
T3_DEF_M5_TRIM_THRESH  = -0.5

def calc_dynamic_trim_thresh(tier: int, worst_roi: float) -> float:
    """★ V10.31AO: T2 디펜스는 T2_DEFENSE_LADDER로 일원화.
    
    이 함수는 일반 TRIM 임계 (디펜스 사다리 미진입 케이스).
    사다리 첫 단계 -1.5 worst → +0.5 TRIM 적용은 호출부에서 별도 처리.
    
    DCA 체결 시 worst_roi=0 리셋되므로 tier별 독립 평가.
    """
    return TRIM_BLENDED_ROI_BY_TIER.get(tier, 1.0)

# ★ V10.31k: Portfolio TP (Peak Trail + Tiered Drop, J안)
# ★ V10.31y: 즉시 arming + T3_3H/T3_8H 시간컷 대체
# 철학: MR = 횡보장 평균 회귀 베팅 → portfolio drop = 횡보장 이탈 시그널
# 임의 시간 컷(3h/8h) 제거, 실제 drop 감지 시 즉시 대피
# ★ V10.31k: Portfolio TP (Peak Trail + Tiered Drop, J안)
# ★ V10.31y: 즉시 arming + T3_3H/T3_8H 시간컷 대체
# ★ V10.31z 긴급 수정: arm 0.0 → 0.3 (ReduceOnly -2022 방지)
# 근거: arm=0.0으로 무제한 발동 → 기존 TRIM preorder와 ReduceOnly 경쟁
# 해결: peak 0.3% 도달 후 arming (자연 쿨다운 역할)
# + PTP step 0 진입 시 모든 기존 reduce preorder 강제 취소 (planners.py)
# ★ V10.31AE: arm 0.3 → 0.0 (사용자 A안) — 시작 직후부터 peak trail 활성
# 근거: HEDGE/TREND off 상태에서 MR 단독 구조는 추세 역방향 시 대책 없음
# 방어: V10.31z에 구현된 "PTP step 0 진입 시 reduce preorder 전량 취소" 로직 유지
# 추가 안전장치: PTP_COOLDOWN_SEC=3600 — 발동 후 1시간 재arming 차단 (잔고 noise trigger 방지)
PTP_PEAK_TRIG_PCT         = 0.0   # ★ V10.31AE: 0.3 → 0.0 (세션 시작 즉시 arming)
PTP_AVG_TIER_GATE         = 0.0   # 모든 포지션 허용
PTP_COOLDOWN_SEC          = 3600  # ★ V10.31AE: 발동 후 1시간 쿨다운 (trigger 시각 기준)
# ★ V10.31AM3 hotfix-9 → hotfix-11 회귀 [04-27]
#   사용자 결정 [04-27 후반부]: "MR의 진입 알파를 믿어야지" — 컨셉 회귀
#   배경: hf-9 도입 시뮬 [실측 25시간]: 17건 차단 매칭 10건 +$19.37 (모두 익절)
#         vs PTP 회피 +$15 = 순 -$4 자해
#   진단: PTP 직후 회복 시기 진입이 MR 진입 알파 강함 (T1 WR 83%, 04-26~27 100%)
#         시간 기반 cooldown은 환경 무관 = 평상시 자해 + 변동성 시기만 가치
#   결정: hf-9 비활성 (값 0). MR 시그널 진입 알파 보존, 손절은 T3 사다리(hf-4) + PTP(hf-5)에 위임
#   1주 후 재검토: hf-10 BTC context 데이터로 정밀 cooldown 조건 결정 (예: 1h ≤ -1.5% 시만)
PTP_ENTRY_COOLDOWN_SEC    = 7200  # ★ V10.31AM3 hotfix-20: hf-9 부활 (2h) — 04-29 시뮬 데이터 PTP3 차단 효과

# ★ V10.31AO-hf14 [05-04]: BTC 추세 기반 불리 포지션 청산 (사용자 통찰)
#   "0.5로 하고 유리한 포지션은 두고 불리한 포지션만 청산"
#   매 틱 BTC 1h 변동률 ±0.5% 도달 시 추세 반대 방향 MR 보유 즉시 시장가 청산
#   유리 방향 보유는 유지 (회복 가능)
#   [실측] 5일 검증: LDO 케이스 +$24 절감, SUI 케이스 +$4.58 익절 보존
# ★ V10.31AO-hf15 [05-04]: BTC 시계열 + 보유 상태 매 1분 기록 (실전 청산 X)
# 사용자 통찰: "BTC 차트만 기록해두면 거기서 조건은 찾으면 되지"
# 1~2주 데이터 누적 → 사후 분석으로 임계/단위/패턴 찾기
# 실전 청산 X — 사다리 5단으로 깊은 손실 자동 차단
# ★ V10.31AM: drop 0.5 → 0.6 상향 — 실측 4일 분석: 0.5%p는 평상시 자주 찍힘 + 방어 미작동
#   (발동 2건 중 1건 false positive, 1건 하락 지속 중 limit 미체결 → taker 시장가 컷)
# 근거: 4일 일내 max drop 1.53/2.13/2.94/0.23% — 0.5%는 noise 영역, 0.6부터 의미 있는 drop
# ★ V10.31AM3 hotfix-5: 0.6 → 0.8 상향 [04-27]
#   사용자 분석: T3 단독 -4% → 잔고 -0.50%p, T3 -4.5% HARD_SL → -0.56%p
#   즉 T3 사다리 -4.5% HARD_SL이 PTP 0.6 트리거 직전 (분업 경계 종이 한 장)
#   문제: 다른 슬롯이 -0.1%p만 같이 빠져도 PTP가 사다리보다 먼저 발동 → T3 사다리 무력화
#   해결: PTP 0.8로 완화 → T3 사다리 작동 영역 확장 (-4.5% → -6.4%까지 사다리 우선)
#   사용자 결정 [04-27]: "0.8이 맞겠지 슬롯이 하나만 차는 경우는 없으니까"
#   효과: hotfix-4 T3 다단계 사다리가 의도대로 작동, PTP는 진짜 portfolio 비상시만
PTP_DROP_BY_PEAK = [
    (0.0, 0.8),   # ★ V10.31AM3 hotfix-5: 0.6 → 0.8 (T3 사다리와 분업 경계 확보)
]

def _ptp_get_drop_thresh(peak_gain_pct: float):
    """★ V10.31k: Peak 기반 tiered drop 임계 (%p 단위).
    
    peak_gain_pct: % 단위 (예: 1.5 = 1.5%)
    반환: %p 단위 drop 임계 (예: 0.4 = 0.4%p)
    peak < PTP_PEAK_TRIG_PCT → None (미arming)
    """
    for p_thresh, d_thresh in PTP_DROP_BY_PEAK:
        if peak_gain_pct >= p_thresh:
            return d_thresh
    return None

# ★ V10.31AM: 단계적 청산 간소화 — 1분 간격 × 2 step (limit 0.05% → 시장가)
# 근거: 기존 4 step × 5분 = 15분 소요 → 실측상 premium limit 대부분 미체결 (하락 중 역방향 limit)
#   PTP 1 (04-24 08:23): 6 positions 중 2개만 청산, 15분 지연 중 반등으로 false positive
#   PTP 2 (04-24 11:17): 3 positions 중 2개만 청산, 추가 -0.24% 하락 후 taker
# 설계 변경: limit 체결 거의 없으니 1분만 기다려 보고 시장가로 확실히 컷
PTP_STEP_INTERVAL_SEC     = 60    # ★ V10.31AM: 300 → 60 (5분 → 1분)
PTP_PREMIUMS_BY_STEP      = {
    0: 0.0005,  # ★ V10.31AM: 0.05% premium (운 좋으면 체결)
    # step 1: 시장가 (premium 없음, taker 확실 컷)
}
# ★ V10.31AL: PTP_SESSION_TZ_OFFSET_SEC 제거 — V10.31AH에서 자정 세션 리셋 제거되며 이 상수도 dead

# ═══════════════════════════════════════════════════════════════════
# ★ V10.31AN: PTP 트리거 모드 [04-30]
# ═══════════════════════════════════════════════════════════════════
# 사용자 결정 [04-30]: 운영 중 config flag로 전환 가능하게
#   "peak_drop" : V10.31k~AM3 기존 — 잔고 peak 대비 drop ≥ PTP_DROP_BY_PEAK
#   "defense_close" : 신규 — T3 사다리/T4 디펜스 청산 ROI ≤ PTP_DEFENSE_ROI_THRESH 시 트리거
#                     트리거는 strategy_core.apply_order_results의 hook에서 외부 설정
#                     _ptp_update_state는 lifecycle만 관리
#
# 의미 변화 [필수 인지]:
#   peak_drop     = portfolio-level 시스템 시그널 (잔고 drop 감지, 선제적)
#   defense_close = single-position cascade 실패 시그널 (사다리 컷 실패 후 정리, 사후적)
#   → 두 모드는 다른 시그널을 감지하며 발동 빈도/특성이 근본적으로 다름
#
# 발동 빈도 [실측 8일 04-22~29 log_tail]:
#   peak_drop:     6회+ (drop 0.8%p 빈번)
#   defense_close: 0회 (사다리 정상 작동 시 -1.4 ~ -2.9% 컷 → -3% 임계 미도달)
#   → defense_close는 사실상 사다리 완전 실패(HARD_SL worst≤-4.5%) 또는
#      hedge_engine T4 깊은 컷(예: ARB T3_DEF_TP -7.4%) 케이스에서만 발동
#
# 양 모드 공통 (영향 받지 않음):
#   - _ptp_active_syms : trigger 활성 시 trim/preorder 차단 (V10.31AJ)
#   - 2-step 청산      : limit 0.05% 60s → 시장가 (V10.31AM)
#   - PTP_COOLDOWN_SEC = 3600s : 발동 후 1h 재트리거 차단
#   - PTP_ENTRY_COOLDOWN_SEC = 7200s : PTP_COMPLETE 후 신규 진입 2h 차단 (hf-20)
#
# 전환 시 주의:
#   - "peak_drop" → "defense_close" : peak/drop 추적 데이터(_ptp_session_start, _ptp_peak_balance)
#                                     는 보존되지만 트리거 판정에 사용 안 됨 (대시보드 표시용)
#   - "defense_close" → "peak_drop" : 즉시 peak/drop 판정 활성화. peak 갱신 따라 자연 arming
PTP_TRIGGER_MODE = "shadow_only"  # ★ V10.31AO-hf15 [05-04]: PTP 실전 비활성, BTC 시계열 로깅만
# 사용자 결정 [05-04]:
#   "PTP 비활성하고 로그로만"
#   "BTC 차트 기록해두면 거기서 조건은 찾으면 되지 왜 미리 정하려고 해"
# 5일 [실측] PTP 효과 -$34 (음수). 사다리 5단으로 깊은 손실 차단 가능.
# 1~2주 BTC 시계열 + 보유 데이터 누적 → 사후 분석으로 진짜 조건 탐색
# 모드: "peak_drop" | "defense_close" | "shadow_only"

# defense_close 모드 전용 파라미터
PTP_DEFENSE_ROI_THRESH = -2.0  # ★ V10.31AO: -3.0 → -2.0 (T2 사다리에 정합, T2_DEF_HARD_SL close -3% 캐치)
PTP_DEFENSE_TRIGGER_REASONS = ("T2_DEF",)  # ★ V10.31AO: T3_DEF_* → T2_DEF prefix (T2_DEF_SL/HARD_SL/TRIM 모두 매칭)
# 매칭 로직: intent.reason.startswith(prefix) — "T3_DEF_SL(worst=...)" 형태와 매칭
# 포함:   T3_DEF_SL    (planners.py plan_t3_defense_v2 + hedge_engine.py T4)
#         T3_DEF_HARD_SL (planners.py worst≤-4.5% 즉시 컷)
#         T3_DEF_TP    (hedge_engine.py T4 — 깊은 worst에서 TP 임계 도달 시 cut)
# 제외:   HARD_SL_T1/T2/T3 (T3 사다리 도달 전 컷 — 사용자 결정 [04-30])
#         T3_DEF_TRAIL    (T4 정상 trail-out — 사다리 청산 아님)
#         T3_DEF_TRIM     (부분 청산 — 사다리 진행 중)

# ★ V10.26: 쿨다운 대폭 단축 — 빠른 평단 압축으로 SL 방지
DCA_COOLDOWN_BY_TIER = {2: 0, 3: 0, 4: 0}  # ★ V10.29b: 쿨다운 전면 제거
DCA_COOLDOWN_SEC     = 0     # 레거시 호환용

# ═══════════════════════════════════════════════════════════════════
# ★ V10.31AO [04-30]: HARD_SL 진입 쿨다운 (연쇄 사망 차단)
# ═══════════════════════════════════════════════════════════════════
# 사용자 결정 [04-30]: 추세장 연쇄 사망 방지 — PTP + 쿨다운 둘 다 적용
#
# [실측 19일] 연쇄 사망 패턴:
#   - 5분 내 다수 SL 9회 (PTP가 잡음)
#   - 1시간 내 3건+ SL 6회 (쿨다운이 잡음)
#   - 일별 큰 손실 (04-19 -$203, 04-13 -$182, 04-23 -$148): 반복 진입 패턴
#
# 작동 메커니즘:
#   - HARD_SL_T1/T2 또는 T2_DEF_HARD_SL/SL 발동 시 _hard_sl_history에 ts 기록
#   - 이후 plan_open에서 최근 HARDSL_COOLDOWN_SEC 내 HARD_SL이
#     HARDSL_COOLDOWN_MIN_COUNT 건 이상이면 신규 진입 차단
#   - 차단 효과는 시간 윈도우 내내 — 진입 중인 포지션은 영향 없음 (PTP 영역)
#
# 보수 기준 (1시간 내 2건 이상이면 30분 차단):
HARDSL_COOLDOWN_SEC          = 1800   # 신규 진입 차단 시간 (30분)
HARDSL_COOLDOWN_WINDOW_SEC   = 3600   # 최근 N초 윈도우에서 HARD_SL 카운트 (1시간)
HARDSL_COOLDOWN_MIN_COUNT    = 2      # 윈도우 내 N건 이상이면 차단 발동

# ═══════════════════════════════════════════════════════════════════
# TP / Trailing
# ═══════════════════════════════════════════════════════════════════
TP1_PCT = 1.8   # ★ v10.8: 방어형 — 빠른 확정 (레거시, 미사용)

# ★ V10.27: TP1 고정 threshold (ROI%) — worst_roi/ATR 스케일링 전부 제거
# T1~T3: 고정값. T4만 worst_roi+2.0 (plan_tp1에서 처리)
# ★ V10.29: T3/T4 TP 두배
# ★ V10.31AM3: T1 2.0→1.5, T2 1.5→1.0 (사용자 결정 [04-26], 옵션 B)
#   근거 [실측, 04-21~04-26]:
#     - T1 MR 107건 중 25건이 1.5%+ 도달 후 TP1 미달로 force_close (미끄러짐)
#     - 16건은 2.0%+ 도달했다 미끄러짐 (WLD 1.73→0.12, ETH 1.85→1.87 등)
#     - T1 max_roi 1.5~2.0 구간에 65건 (60.7%) 집중 → "여기서 잡아야"
#     - T2는 표본 작지만 사용자 직관 우선 (스캘핑 컨셉 일관성)
#   추정 효과:
#     - 수익 영향: -$30 (큰 이익 잘림 25건 × 0.5%)
#     - 리스크 영향: +$170 (미끄러짐 회피)
#     - 순 +$140 + WR +8%p + 분산 ↓
#   롤백 시: T1 2.0, T2 1.5로 복귀
TP1_FIXED = {1: 1.5, 2: 1.0, 3: 2.4, 4: 1.6}  # T1 1.5%, T2 1.0% — 미끄러짐 방어 + 분산 ↓

# ★ V10.27c: HARD_SL = DCA 트리거 -1% / T4는 체결가 -2%
# ★ V10.29: SL = 다음 DCA 트리거 - 2%
# ★ V10.31AM3 hotfix-17: HARD_SL_T2 -5.6% → -3.0% (사용자 결정 [04-29], 보험 차원)
#   배경: 04-28 TIA 자해 -$23.67 — T2 사이즈에 -5.6% 임계 적용으로 깊은 손실
#     hf-17 잔량 기반 tier 보정으로 큰 사이즈 → T3 임계 적용되지만, 진짜 T2 사이즈일 때는
#     -5.6%까지 기다림 = 여전히 깊은 손실 가능
#   사용자: "T2 SL도 -3으로 바꿔 보험 차원에서"
#   변경: T2 임계 -5.6% → -3.0% (사다리 -3.0% SL 영역과 정합)
#   효과: T2 사이즈 -3.0% 도달 시 즉시 cut → 손실 cap
#   롤백: HARD_SL_BY_TIER = {1: -3.8, 2: -5.6, 3: -10.0}
HARD_SL_BY_TIER = {1: -2.5, 2: -4.0, 3: -5.5}  # ★ V14 [05-06]: T2 -3.0 → -4.0 (사용자: T3 DCA -3.6 통과 위해, T3 fill 실패 케이스 안전망)

# ═══════════════════════════════════════════════════════════════════
# ★ V10.29: Counter Signal — MR ROI- + 일목구름 돌파 → 반대 사이드 MR 진입
#   진입만 다르고 DCA/TP/SL/Trail은 기존 MR과 동일 (role=CORE_MR)
# ═══════════════════════════════════════════════════════════════════
COUNTER_ENABLED       = True
COUNTER_ROI_THRESH    = -2.0    # ★ V10.29b: -2.0으로 완화 (스윙 여유)
COUNTER_SIZE_RATIO    = 1.0     # MR T1과 동일 사이즈
COUNTER_COOLDOWN_SEC  = 600     # 10분 쿨다운 (중복 진입 방지)
COUNTER_MAX           = 2       # 동시 counter 포지션 최대
# T1~T3: 평균 EP 기준 / T4: T4 체결가 기준 (planners에서 분기)

# ★ V10.26b 레거시 (plan_tp1에서 미사용, _manage_tp1_preorders 호환용)
REBOUND_ALPHA = {1: 3.5, 2: 3.0, 3: 2.5, 4: 2.0}

# ★ v10.8: DCA 깊을수록 빨리 탈출 (레거시, TP1_PREORDER fallback용)
TP1_PCT_BY_DCA = {
    1: 1.5,    # ★ V11: T1 +1.5% (손익비 1:1.5, T1 단일 진입)
}
TP_ATR_POWER    = 0.3
TP_ATR_MIN_MULT = 0.7
TP_ATR_MAX_MULT = 1.5

TP1_PARTIAL_RATIO  = 1.0  # ★ V10.31c: 100% 전량 청산 (40% 부분청산 → 남은 60% trail 이중처리 제거)
# ★ V10.29e: TP2 제거 — planners에서 TP2 미생성, 죽은 코드
TRAILING_TIMEOUT_MIN = 45

# ★ V10.27e: 심볼별 최소 주문 수량 (하드코딩 통합)
SYM_MIN_QTY = {
    "ETH/USDT": 0.001, "BNB/USDT": 0.01, "SOL/USDT": 0.1,
    "BTC/USDT": 0.001, "AVAX/USDT": 0.1,
}
SYM_MIN_QTY_DEFAULT = 1.0

# ═══════════════════════════════════════════════════════════════════
# OPEN / CORR
# ═══════════════════════════════════════════════════════════════════
HEDGE_OPEN_CORR_MIN     = 0.40   # ★ V10.29: 0.6→0.40 (진입과 동일 — FET 헷지 15회 REJECT 방지)
OPEN_CORR_MIN           = 0.50   # ★ V10.31AM: 0.60 → 0.50 (3시간 corr 기준 완화 — 단기 값 절대적으로 낮음)
# ★ V10.31AO [04-30]: 30분 corr 진입 필터 추가 (혼자 튀는 놈 사전 식별)
#   사용자 결정: 3h corr은 너무 길음 — 진입 직전 30분 BTC 상관성으로 디커플링 감지
#   [실측] 1초 윈도우 동시 진입 SL 3% vs 단독 18.2% — 단독은 BTC 디커플링 시 위험
#   30분 corr ≥ 0.50 통과한 심볼만 OPEN 허용. corr_30m 없으면 corr_3h fallback.
OPEN_CORR_MIN_30M       = 0.60   # ★ V10.31AO-hf8 [05-02]: 0.5 → 0.6 환원 — 사용자 의도 정정 (시뮬은 0.5, 운영은 0.6)
                                 #   기존 2일 corr 기준 0.60 → 3시간 corr 기준 0.50으로 재조정
                                 #   극단 decoupling(OP 같은 corr 0.2~0.3)은 여전 차단, 정적 구간 노이즈는 통과
HEDGE_STAGE1_MULTIPLIER = 1.4
HEDGE_STAGE2_MULTIPLIER = 2.4
HEDGE_MAX_MULTIPLIER    = 3.0
HEDGE_PROFIT_CLOSE_PCT  = 3.0   # 헷지 포지션 익절 ROI% 기준

# ═══════════════════════════════════════════════════════════════════
# 레짐 판단
# ═══════════════════════════════════════════════════════════════════
REGIME_LOW_BTC6H_MAX      = 0.02
REGIME_LOW_ATR_RATIO_MAX  = 1.5
REGIME_HIGH_BTC6H_MIN     = 0.05
REGIME_HIGH_ATR_RATIO_MIN = 2.5

# ═══════════════════════════════════════════════════════════════════
# Universe ASYM v2  — ★ v10.15: 롱/숏 분리 파이프라인
# ═══════════════════════════════════════════════════════════════════
# 공통
UNIVERSE_MAX_CORR        = 0.96
UNIVERSE_CORR_WHITELIST  = {"ETH/USDT", "SOL/USDT", "BNB/USDT", "LINK/USDT"}
UNIVERSE_VOL_FLOOR_USD   = 500_000   # 절대 최저 (이하는 무조건 제외)
UNIVERSE_TOP_N           = 16
UNIVERSE_EXCLUDE_TOP_ATR = 0
UNIVERSE_LONG_N          = 8
UNIVERSE_SHORT_N         = 8
UNIVERSE_STICKY_MIN_SEC  = 600
UNIVERSE_MIN_POOL_SIZE   = 3

# ★ v10.15: 롱/숏 분리 파라미터
LONG_MIN_CORR   = 0.50
LONG_BETA_MIN   = 0.80
LONG_BETA_MAX   = 2.00
SHORT_MIN_CORR  = 0.40   # ★ V10.27f: 0.50→0.40 (숏 유니버스 확대)
SHORT_BETA_MIN  = 0.50   # ★ V10.27f: 0.70→0.50 (저베타 숏 허용)
SHORT_BETA_MAX  = 2.00

# ═══════════════════════════════════════════════════════════════
# ★ V10.31e: 심볼별 실적 기반 동적 조정 (Phase 3b)
# 과거 실적 = 후행지표(lagging). 과적합 리스크 인지한 상태에서
# 사용자 판단으로 진행. 문제시 SYMBOL_STATS_ENABLED=False로 즉시 원복 가능.
# ═══════════════════════════════════════════════════════════════
SYMBOL_STATS_ENABLED       = True    # 전체 feature 스위치
SYMBOL_STATS_WINDOW_DAYS   = 7       # 실적 집계 창 (일)
SYMBOL_STATS_MIN_SAMPLES   = 5       # 최소 거래 건수 (이하는 중립 판단)
# 손실 심볼 쿨다운
SYMBOL_COOLDOWN_PNL_THRESH = 0.0     # 총 PnL < 0 이면 쿨다운 후보
SYMBOL_COOLDOWN_DAYS       = 3       # 쿨다운 기간 (일)
# 선발 tiebreaker
SYMBOL_PNL_WEIGHT          = 0.20    # ATR 랭킹 대비 PnL 가중치 (0.0=비활성)
# ═══════════════════════════════════════════════════════════════

# ★ v10.15: HIGH 레짐 sticky
HIGH_STICKY_SEC = 300   # HIGH 진입 후 5분간 유지

# ★ v10.15: MinROI JSON 상태 파일
MINROI_FILE     = "v9_minroi.json"
RECONCILE_INTERVAL_SEC = 45   # 바이낸스 대조 주기 (현재 미사용, 매틱 sync)

# ★ v10.15: Skew MR 추가 진입
SKEW_HEDGE_TRIGGER = 0.12   # ★ PATCH: 15%→12% (시장가 전환 + calc_skew 헷지 제외로 안정화)

# ═══════════════════════════════════════════════════════════════
# ★ V10.22: Skew 관련 — hedge_core에서 사용
# (기존 TP_LOCK 계열 삭제 → _skew_tp_adjustment()로 대체)
# ═══════════════════════════════════════════════════════════════
SKEW_STAGE2_TRIGGER     = 0.15  # hedge_core: 2단계 헷지 트리거
SKEW_STAGE2_TIMEOUT_SEC = 900   # 15분: stage2 지속 → 헷지 필요조건
SKEW_HEDGE_STRESS_ROI   = -3.0  # heavy side ROI 이하 → 헷지 조건 완화

# ★ V10.27: INSURANCE_SH — BTC 급변 직접 감지 기반 (DCA 차단 연동 제거)
INSURANCE_BTC_1M_THRESH = 0.020   # ★ V10.29b: 1분 ±2.0% — 극단적 급락/급등만
INSURANCE_BTC_3M_THRESH = 9.999   # 비활성화 (도달 불가)
INSURANCE_BTC_5M_THRESH = 9.999   # 비활성화 (도달 불가)
INSURANCE_SIZE_RATIO    = 0.5     # 소스 50%
INSURANCE_COOLDOWN_SEC  = 600     # 10분 글로벌 쿨다운
INSURANCE_MIN_AFFECTED  = 2       # affected side 최소 포지션 수
INSURANCE_TP_ROI        = 3.0     # 수익 시 trailing 전환 기준
INSURANCE_CUT_ROI       = -1.0    # 10분 후 손실 컷 기준
INSURANCE_MAX_HOLD_SEC  = 1200    # 20분 절대 상한

GLOBAL_BLACKLIST = [
    "BTC/USDT", "DOGE/USDT", "SHIB/USDT", "PEPE/USDT",
    "FLOKI/USDT", "BONK/USDT", "WIF/USDT", "1000PEPE/USDT",
    "LUNC/USDT", "USTC/USDT", "FTM/USDT", "SNX/USDT",
]

# ★ v10.5: 심볼별 방향 바이어스
# LONG_ONLY  — 숏 진입 금지
# SHORT_ONLY — 롱 진입 금지
# NEUTRAL    — 양방향 허용
LONG_ONLY_SYMBOLS = {
    # ★ V10.31h: 시나리오 A 축소 — 200회 universe 갱신 100% LONG 일관 + 메이저
    "BNB/USDT", "XRP/USDT", "XLM/USDT", "AVAX/USDT",
    # 제거: TRX, TON, ICP, ETC (200회 0회 등장 — corr/beta filter 상시 컷)
}

SHORT_ONLY_SYMBOLS = {
    # ★ V10.31h: 시나리오 A 축소 — 200회 universe 갱신 100% SHORT 일관 + 토크노믹스 명확
    "TIA/USDT", "FET/USDT", "OP/USDT", "INJ/USDT", "WLD/USDT", "FIL/USDT",
    # ARB, SEI, ATOM → NEUTRAL 이동 (등장은 하지만 펀딩 자연 분리에 맡김)
    # 제거: STRK, RUNE, RNDR, AGIX, AKT, GRT (200회 0회 등장)
}

NEUTRAL_SYMBOLS = {
    # ★ V10.27f: 대형주 NEUTRAL 이동 (하락장 숏 허용)
    "ETH/USDT", "SOL/USDT", "LINK/USDT", "ADA/USDT", "DOT/USDT",
    # 기존 NEUTRAL (200회 등장 확인된 것만)
    "SUI/USDT", "APT/USDT", "NEAR/USDT", "ATOM/USDT", "UNI/USDT",
    # ★ V10.31h: SHORT_ONLY → NEUTRAL 이동 (등장 확인됨, 펀딩 자연 분리에 위임)
    "ARB/USDT", "SEI/USDT",
    # ★ V10.31h: 신규 추가 7종 — 메이저 알트 (DeFi/Solana/AI/BTC eco)
    "LDO/USDT", "PENDLE/USDT", "JUP/USDT", "JTO/USDT",
    "ARKM/USDT", "GMX/USDT", "ORDI/USDT",
    # ★ V10.31h: 제거 (200회 0회): AAVE, STX, MATIC, EOS
}

# 전체 유니버스 (합집합)
MAJOR_UNIVERSE = sorted(LONG_ONLY_SYMBOLS | SHORT_ONLY_SYMBOLS | NEUTRAL_SYMBOLS)

# ═══════════════════════════════════════════════════════════════════
# 로그 / 상태 파일
# ═══════════════════════════════════════════════════════════════════
LOG_DIR            = "v9_logs"
LOG_INTENTS_FILE   = "log_intents.csv"
LOG_RISK_FILE      = "log_risk.csv"
LOG_ORDERS_FILE    = "log_orders.csv"
LOG_FILLS_FILE     = "log_fills.csv"
LOG_POSITIONS_FILE = "log_positions.csv"
LOG_UNIVERSE_FILE  = "log_universe.csv"
# ★ V10.31c: LOG_SKEW_FILE 제거 (스큐 로직 V10.30에서 전면 삭제 — 죽은 로깅)
STATE_FILE         = "v9_state.json"
HEARTBEAT_FILE     = "heartbeat.txt"

ACTIVATION_THRESHOLD = 1800.0

# ═══════════════════════════════════════════════════════════════════
# 방향별 총 노출 캡  (v9.8 신규)
# ═══════════════════════════════════════════════════════════════════
# Long 합산 명목 > equity × 1.8  → 신규 Long 금지
# Short 합산 명목 > equity × 1.8 → 신규 Short 금지
# 양방향 합산   > equity × 2.6  → 방향 불문 신규 금지
EXPOSURE_CAP_DIR   = 1.8   # 방향별 상한 (equity 배수)
EXPOSURE_CAP_TOTAL = 2.6   # 양방향 합산 상한

# ASYM 커버 비율  size = imbalance × 0.75
#   100%면 롱 반등 시 숏 손실로 이익 상쇄 → 75% 부분 커버
#   0.7 미만: 커버 부족 → 진입 허용
#   1.5 초과: 독립 베팅 수준 → 진입 거부
ASYM_COVER_RATIO_MIN = 0.7
ASYM_COVER_RATIO_MAX = 1.5
ASYM_SIZE_RATIO      = 0.75
ASYM_MAX_DCA_LEVEL   = 2

# ★ v10.4: MR 실패 ASYM (T2+max_roi=0 트리거)
ASYM_OPEN_RATIO      = 0.30   # T2 트리거 시 초기 사이즈
# 소스 DCA 레벨별 ASYM 누적 사이즈 비율 (grid_notional 대비)
ASYM_DCA_SIZE = {2: 0.30, 3: 0.50, 4: 0.75}
# 알파 슬롯 조건
ASYM_ALPHA_MR_MAX    = 0.60   # margin_ratio < 0.60
ASYM_ALPHA_IMBAL_MIN = 0.30   # 롱숏 노출 비대칭 ≥ 30%
# ASYM DCA 허용 마진율 상한 (이 이상이면 킬스위치가 막음)
ASYM_DCA_MR_MAX      = 0.90

# ═══════════════════════════════════════════════════════════════════
# 헤지모드  (★ v9.9 신규 — 바이낸스 헤지모드 전환 완료)
# ═══════════════════════════════════════════════════════════════════
HEDGE_MODE = True   # True → positionSide 태깅 활성화

# ═══════════════════════════════════════════════════════════════════
# Falling Knife Filter  (★ v9.9 신규 → V10.31e-4 비활성화)
# 9일 실측으로 효과 없음 확인 (MR T3 FC 5.8% > TREND 4.4%) 후 planners.py에서
# 호출/함수/import 제거. 이 상수는 참조 없으나 히스토리 기록 겸 유지.
# 필요 시 롤백 포인트.
# ═══════════════════════════════════════════════════════════════════
FALLING_KNIFE_BARS      = 3      # (미사용) 최근 N개 5m 봉
FALLING_KNIFE_THRESHOLD = 0.020  # (미사용) 누적 변화 임계값

# ═══════════════════════════════════════════════════════════════════
# Pullback Entry  (★ v9.9 신규)
# ═══════════════════════════════════════════════════════════════════
PULLBACK_DIST_ATR     = 1.0  # Pullback 기준 배수 (ATR 연동의 중앙값)
PULLBACK_ATR_POWER    = 0.4  # 연동 강도 (0=고정, 1=완전연동)
PULLBACK_ATR_MIN_MULT = 0.6  # 최소 배수 — 횡보 시 하한 (너무 얕은 진입 방지)
PULLBACK_ATR_MAX_MULT = 1.6  # 최대 배수 — 추세 시 상한 (얕은 눌림 필터)

# ═══════════════════════════════════════════════════════════════════
# 추세 필터  (v9.8 신규)
# ═══════════════════════════════════════════════════════════════════
# 1h EMA20 vs EMA50 크로스 기반
#   EMA20 > EMA50 + deadzone → 상승추세 → Short 신규 OPEN 차단
#   EMA20 < EMA50 - deadzone → 하락추세 → Long  신규 OPEN 차단
#   deadzone 이내           → 애매 구간 → 양방향 허용
# TREND_FILTER_DEADZONE: EMA20/50 차이 ÷ 가격 기준 (±0.5% = 0.005)
# 크게 잡을수록 필터가 약해져 진입 빈도 유지
TREND_FILTER_ENABLED  = False   # ★ v9.9: 1h 추세 차단 필터 제거
TREND_FILTER_DEADZONE = 0.005
TREND_FILTER_MIN_BARS = 50


# ═════════════════════════════════════════════════════════════════
# ★ V10.29: 공유 계산 함수 — strategy_core / runner / planners 통합
#   중복 구현으로 인한 불일치 방지
# ═════════════════════════════════════════════════════════════════

def calc_trim_price(blended_ep: float, side: str, tier: int, worst_roi: float = 0.0) -> float:
    """★ V10.29b: Trim 선주문 목표 가격 — 블렌디드 EP 기준.
    
    ★ V10.31j: worst_roi 인자 추가 — 디펜스 모드 시 동적 임계.
    기본값(worst_roi=0) 시 기존 TRIM_BLENDED_ROI_BY_TIER 그대로.
    """
    roi_pct = calc_dynamic_trim_thresh(tier, worst_roi)
    if side == "buy":
        return blended_ep * (1 + roi_pct / LEVERAGE / 100)
    return blended_ep * (1 - roi_pct / LEVERAGE / 100)


def calc_tier_notional(tier: int, bal: float) -> float:
    """★ V10.29d: 특정 tier까지 누적 목표 노셔널 (USDT).
    tier=0 → 0, tier=1 → T1 비중, tier=2 → T1+T2 비중, ...
    bal = real_balance_usdt (총 자본)
    """
    if tier <= 0 or bal <= 0:
        return 0.0
    total_w = sum(DCA_WEIGHTS)
    if total_w <= 0:
        return 0.0
    cum_w = sum(DCA_WEIGHTS[:min(tier, len(DCA_WEIGHTS))])
    grid_notional = bal / GRID_DIVISOR * LEVERAGE
    return grid_notional * cum_w / total_w


def notional_to_qty(target_notional: float, price: float) -> float:
    """★ V10.29d: 노셔널 → 수량 변환. price=0이면 0 반환."""
    if price <= 0 or target_notional <= 0:
        return 0.0
    return target_notional / price


def calc_tier_from_amt(amt: float, price: float, bal: float) -> int:
    """★ V10.31AM3 hotfix-17: 잔량 기반 dca_level 역산.
    
    TRIM 후 잔량(amt)이 어느 tier 사이즈에 해당하는지 결정.
    
    ★ V10.31AO-hf7 [05-01]: DCA_WEIGHTS 길이 동적 처리
       사용자 통찰 [05-01]: "T3로 텔레그램에 표시된게 가장 이상해"
       원인: V10.31AO는 [33,67] (2단)인데 함수가 [33,33,34] (3단) 가정
              → calc_tier_notional(3) = T2와 동일 ($1170)
              → TIA 잔량 $1066 → T3 잘못 반환 (T2 80% 임계 통과)
              → 텔레그램 "TIA T3" 잘못 표시
       수정: MAX_TIER = len(DCA_WEIGHTS)로 상한 결정
    
    사용자 통찰 [04-29]: "트림 잔량이 남아있으면 그 티어 유지하면 깔끔한 거 아닌가"
    → 임의 임계 X. 잔량 사이즈 = tier 결정.
    
    Args:
        amt: TRIM 후 잔량 (qty)
        price: 현재 가격 (notional 계산용)
        bal: 잔고 (tier 사이즈 계산용)
    
    Returns:
        1 ~ len(DCA_WEIGHTS) 중 하나
    """
    if amt <= 0 or price <= 0 or bal <= 0:
        return 1
    
    remaining_notional = amt * price
    
    # ★ V10.31AO-hf7: MAX_TIER = DCA_WEIGHTS 길이
    _MAX_TIER = len(DCA_WEIGHTS)
    
    # 큰 tier부터 확인 (잔량이 클수록 큰 tier)
    for _t in range(_MAX_TIER, 0, -1):
        _target = calc_tier_notional(_t, bal)
        if _target > 0 and remaining_notional >= _target * 0.8:
            return _t
    
    return 1


def calc_trim_qty(total_amt: float, tier: int, ep: float = 0.0, bal: float = 0.0,
                  mark_price: float = 0.0, t1_amt: float = 0.0,
                  t2_amt: float = 0.0, t3_amt: float = 0.0) -> float:
    """★ V10.29d: Trim 수량 — 순수 노셔널 기반 + 안전 캡.

    현재 포지션 노셔널에서 목표 tier 노셔널을 빼서 트림할 수량 계산.
    ★ 안전장치: tier 비중 기반 최대값 초과 방지 (이중 trim 방어)
    
    ★ V10.31AO-hf3 [04-30]: 산만큼 그대로 팔기 (사용자 통찰)
       사용자: "살때 수량 기억했다가 파는게 어려워?"
       원리: TRIM_T2 → t2_amt 그대로 청산 (T2 fill qty 정확히 재사용)
             계산 재실행 없음 → dust 발생 차단
       fallback: tier별 amt 없으면 기존 노셔널 계산 사용
    """
    if tier < 1 or total_amt <= 0:
        return 0.0

    target_tier = tier - 1
    price = mark_price if mark_price > 0 else ep

    # ★ V10.31AO-hf3: 산만큼 그대로 팔기 — tier별 fill qty 우선
    if tier == 2 and target_tier == 1 and t2_amt > 0:
        # T2 fill qty 그대로 청산 (T1 잔량은 자연히 t1_amt)
        # 안전 검증: t2_amt가 total보다 작거나 같아야 정상
        if t2_amt <= total_amt:
            # 잔량 dust 방어
            _residual = total_amt - t2_amt
            if price > 0:
                _residual_notional = _residual * price
                if 0 < _residual_notional < 5.0:
                    return total_amt  # 전량
            return t2_amt
        # t2_amt > total → 비정상 (TRIM 후 다시 trim 등) → fallback
    
    if tier == 3 and target_tier == 2 and t3_amt > 0:
        if t3_amt <= total_amt:
            _residual = total_amt - t3_amt
            if price > 0:
                _residual_notional = _residual * price
                if 0 < _residual_notional < 5.0:
                    return total_amt
            return t3_amt

    # ★ V10.31AO-hf3 (fallback 1): t1_amt sync (T2 → T1, t2_amt 없을 때)
    if t1_amt > 0 and tier == 2 and target_tier == 1:
        _sync_qty = total_amt - t1_amt
        if _sync_qty > 0:
            _ratio = _sync_qty / total_amt
            if 0.3 < _ratio < 0.85:
                if price > 0:
                    _residual_notional = t1_amt * price
                    if 0 < _residual_notional < 5.0:
                        return total_amt
                return _sync_qty

    # ★ V10.29d: tier 비중 기반 최대 trim — 절대 이 이상 안 팔림
    total_w = sum(DCA_WEIGHTS)
    tier_w = DCA_WEIGHTS[tier - 1] if tier <= len(DCA_WEIGHTS) else DCA_WEIGHTS[-1]
    cum_w_current = sum(DCA_WEIGHTS[:min(tier, len(DCA_WEIGHTS))])
    max_trim_ratio = tier_w / cum_w_current if cum_w_current > 0 else 0.5
    max_trim_qty = total_amt * max_trim_ratio * 1.1  # 10% 여유

    # bal 있으면 절대 노셔널 기준
    if bal > 0 and price > 0:
        target_notional = calc_tier_notional(target_tier, bal)
        current_notional = total_amt * price
        trim_notional = current_notional - target_notional
        if trim_notional <= 0:
            return 0.0
        qty = trim_notional / price
        qty = min(qty, max_trim_qty)  # ★ 안전 캡
        # ★ V10.31AM3: 잔량 MIN_NOTIONAL 방어 — 트림 후 잔량 노셔널이 $5 미만이면 전량 매도
        # 근거: 실측 [04-25] ATOM/APT 트림 후 0.01~2.10 lot 잔량 ($0.02~$2.04) 발생
        # 거래소 MIN_NOTIONAL 미달이라 후속 청산 불가 → 사용자 수동 청산 부담
        # 해결: 트림 시 잔량이 $5 미달 예상이면 전량 청산
        residual_notional = (total_amt - qty) * price
        if 0 < residual_notional < 5.0:
            qty = total_amt  # 전량 매도
        return qty

    # fallback: 비율 방식 (bal 없을 때)
    qty = min(total_amt * (tier_w / cum_w_current), max_trim_qty)
    # ★ V10.31AM3: fallback 경로도 동일 방어 (bal 없을 땐 price 기반 미달 검사)
    if price > 0:
        residual_notional = (total_amt - qty) * price
        if 0 < residual_notional < 5.0:
            qty = total_amt
    return qty


def calc_tp1_thresh(dca_level: int, worst_roi: float) -> float:
    """TP1 임계값 (ROI %) — 고정값. V10.29e: urgency/heavy 로직 제거."""
    if dca_level <= 2:
        return TP1_FIXED.get(dca_level, 2.0)
    else:
        _rebound = TP1_FIXED.get(dca_level, 2.0)
        return max(worst_roi + _rebound, _rebound)


def get_sl_entry(p: dict, tier: int) -> float:
    """★ V10.29b: HARD_SL 기준 = 블렌디드 EP (바이낸스 ROI와 동일)."""
    return float(p.get("ep", 0.0) or 0.0)


def calc_dca_trigger_price(ep: float, side: str, tier: int) -> float:
    """★ V10.29e: DCA 선주문 트리거 가격.
    T2: EP × (1 ± 1.8/LEV/100), T3: EP × (1 ± 3.6/LEV/100)
    """
    roi_trig = DCA_ENTRY_ROI_BY_TIER.get(tier, -3.6)
    dist = abs(roi_trig) / 100 / LEVERAGE
    if side == "buy":
        return ep * (1.0 - dist)   # long: 가격 하락 시 DCA
    return ep * (1.0 + dist)       # short: 가격 상승 시 DCA


# ═══════════════════════════════════════════════════════════════
# TREND COMPANION (v10.29c — MR 진입 시 반대 방향 추세 심볼 동시 진입)
# ═══════════════════════════════════════════════════════════════
TREND_ENABLED        = True
# ★ V10.31c: TREND_MIN_SCORE 제거 — 실제 필터는 _TR_MIN=0.5 (planners.py:1073)
# 하드코딩이 유일 사용처였음. config import만 남아있었음.
TREND_MAX_SCORE      = 5.0     # ★ V10.30: score 상한 (과열 역전 방지)
TREND_COOLDOWN_SEC   = 0       # ★ V10.30: 쿨다운 제거. V10.31d-3에서 _open_dir_cd도 전면 제거됨

# ═══════════════════════════════════════════════════════════════
# Beta Cycle (v10.29d — 1h Signal, Short-Only)
# ═══════════════════════════════════════════════════════════════
BC_ENABLED          = False  # ★ V14.9 [05-06]: BC 알파 폐기 — 사용자 결정 "BC도 지워버려". 진입 차단, 활성 BC 포지션은 TP/SL/trail로 자연 청산 후 종료. CB는 별도 결정 (CB_ENABLED 그대로)
CB_ENABLED          = True    # ★ V10.29b: Crash Bounce 알파
BC_ARM_THRESH       = 0.10      # ★ V10.31b: excess return ≥ 10% → ARMED (진짜 과잉만)
BC_BASELINE_WINDOW  = 72        # ★ V10.30: baseline 계산 구간 (72h)
BC_BASELINE_SKIP    = 24        # ★ V10.30: 최근 24h 제외 (스파이크 오염 방지)
BC_SHORT_SL         = 8.0       # SL 8%
BC_SHORT_TP         = 6.0       # TP 6%
BC_TRAIL_ACTIVATION = 0.03      # 3% 수익 시 트레일 시작
BC_TRAIL_FLOOR      = 0.015     # 트레일 최소 1.5%
BC_TRAIL_ATR_MULT   = 1.5       # ATR × 1.5
BC_COOLDOWN_HOURS   = 72        # ★ V10.29d: 3일 쿨다운 (시간 단위)
BC_ENTRY_PER_DAY    = 3         # 하루 최대 진입
BC_MAX_POS          = 2         # ★ 테스트: 슬롯 2개
BC_SIZE_DIVISOR     = 10        # ★ 테스트: equity/10 ≈ 10%
BC_MAX_HOLD_HOURS   = 336       # 14일
BC_ARMED_EXPIRY_H   = 720       # ★ V10.29d: ARMED 만료 (시간, 30일)
BC_PULLBACK_MAX     = 0.08      # ★ V10.31AM3: 5%→8% — 거래량/RSI 진정 검증 추가에 따른 진입 윈도우 확대 (사용자 결정 [04-26], 옵션 A)
BC_PULLBACK_MIN     = 0.005     # 0.5% 미만이면 대기
BC_UNI_TOP_N        = 20        # 일일 유니버스 상위 20개
BC_BETA_WINDOW      = 168       # ★ V10.29d: beta 계산 윈도우 (1h bars = 7일)
BC_RETURN_WINDOW    = 24        # ★ V10.29d: excess return 윈도우 (1h bars = 24시간)
BC_1H_BUFFER_SIZE   = 250       # ★ V10.29d: 1h 봉 버퍼 크기

# ═══════════════════════════════════════════════════════════════
# Crash Bounce (v10.29c — WF 검증 best config)
#   CR5% | 24h8% | VS1.0 | DL0h | SL3% | TR_ATR1.0 | MH48h | B3
#   WR=72% | PF=3.71 | MDD=-2.1% | 12mo OOS $+211
# ═══════════════════════════════════════════════════════════════
CB_CRASH_4H          = -0.05      # BTC 4h ROC ≤ -5% → 크래시
CB_CRASH_24H         = -0.08      # BTC 24h ROC ≤ -8% → 크래시 (단독 트리거)
CB_VOL_SURGE_GATE    = 1.0        # 4h 크래시 시 볼륨 서지 게이트
CB_ENTRY_DELAY_H     = 0          # 크래시 후 진입 대기 (0=즉시)
CB_SL_PCT            = 3.0        # SL 3%
CB_TRAIL_ACTIVATION  = 0.02       # 2% 수익 시 트레일 시작
CB_TRAIL_FLOOR       = 0.01       # 트레일 최소 1%
CB_TRAIL_ATR_MULT    = 1.0        # ATR × 1.0
CB_MAX_HOLD_H        = 48         # 최대 보유 48시간
CB_MAX_POS           = 3          # 동시 최대 포지션
CB_MAX_ENTRIES       = 3          # 크래시당 최대 진입
CB_SIZE_PCT          = 0.10       # equity × 10%
CB_TOP_BETA_N        = 3          # 베타 상위 3개 진입
CB_COOLDOWN_H        = 48         # 크래시 이벤트 쿨다운 48시간
