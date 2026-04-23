"""
V9 Trinity Config  (v10.28 — 2026-04-05 MR 전략 복원 패치)
===============================================
v10.27f → v10.28 변경:
  Heavy DCA 차단      urgency 기반 heavy side DCA 차단 전면 제거
  독립 DCA Trim       gate 조건 제거 → tier entry +2% ROI 달성 시 무조건 trim
  (진입 ATR 패널티 / TP 할인 / light block은 유지)
"""

VERSION = "10.31h"  # ★ V10.31h: NOSLOT A 조건 + universe 시나리오A 축소 + 14제거/7추가

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

TREND_NOSLOT_ENABLED = False  # ★ V10.31AA: MR 슬롯풀 시 다른 심볼 추세 진입
HEDGE_COMP_ENABLED   = False  # ★ V10.31AA: MR 진입 시 동일 심볼 반대 헷지
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
DCA_WEIGHTS = [33, 33, 34]  # ★ V10.31c: 균등 배분 — T3 50% 몰빵 → T1/T2 사이즈 증가로 수익원 강화, T3 최악손실 감소

DCA_LIMIT_TIMEOUT_SEC = 60
DCA_MIN_CORR          = 0.5

# ★ V10.28b: Entry 기준 DCA — 이전 tier 체결가에서 X% 하락 시 트리거
# EP 기준이 아닌 실제 가격 간격 유지 (EP 압축 방지)
DCA_ENTRY_BASED   = False  # ★ V10.29b: 블렌디드 EP 기준 통일 (바이낸스 ROI = 봇 ROI)
DCA_ENTRY_ROI     = -1.8   # 레거시 호환 (T2 기본값)
# ★ V10.29: T3/T4 DCA 거리 두배 — 노이즈 DCA 방지
DCA_ENTRY_ROI_BY_TIER = {2: -1.8, 3: -3.6}  # ★ V10.29b: T4 제거

# ★ V10.29b: Trim — 블렌디드 EP 기준 실제 ROI로 통일
# T3(+0.5%) → T2(+1.0%) → T1 TP(+2.0%) 계단식 익절
TRIM_BLENDED_ROI_BY_TIER = {3: 0.5, 2: 1.5}  # ★ V10.31c: T3 1.0 → 0.5 (DCA 33/33/34 전환으로 blended ROI 약 1%p 더 깊어지는 것 보정)
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
    """★ V10.31j: worst_roi 기반 동적 TRIM 임계.
    
    tier=2 + worst ≤ -2.0 → 0.5 (약반등 TRIM 허용)
    tier=3 + worst ≤ -5.0 → -0.5 (약손실 TRIM 허용)
    그 외 → 기본 TRIM_BLENDED_ROI_BY_TIER 값
    
    DCA 체결 시 worst_roi=0 리셋되므로 tier별 독립 평가.
    """
    if tier == 2 and worst_roi <= T2_DEF_WORST_ENTER:
        return T2_DEF_TRIM_THRESH
    if tier == 3 and worst_roi <= T3_DEF_M5_WORST_ENTER:
        return T3_DEF_M5_TRIM_THRESH
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
PTP_PEAK_TRIG_PCT         = 0.3   # ★ V10.31z: 0.0 → 0.3 (최소 이익 확보 후 arming)
PTP_AVG_TIER_GATE         = 0.0   # 모든 포지션 허용
# Tiered drop — peak 높을수록 drop 허용폭 증가 (상승 여유 인정)
PTP_DROP_BY_PEAK = [
    (2.0, 0.5),   # peak ≥ 2.0% → drop 0.5%p
    (1.5, 0.4),   # peak ≥ 1.5% → drop 0.4%p
    (1.0, 0.3),   # peak ≥ 1.0% → drop 0.3%p
    (0.5, 0.5),   # peak ≥ 0.5% → drop 0.5%p
    (0.3, 0.7),   # ★ V10.31z: peak 0.3~0.5% → drop 0.7%p (arm 최소치)
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

# 단계적 청산 (V10.31j T3_3H 패턴)
PTP_STEP_INTERVAL_SEC     = 300   # 5분 간격
PTP_PREMIUMS_BY_STEP      = {
    0: 0.0020,  # +0.20%
    1: 0.0015,  # +0.15%
    2: 0.0010,  # +0.10%
    # step 3: 시장가 (premium=0)
}
# 일일 세션 경계 — ★ KST 09:00 (UTC 00:00) 통일
# - 텔레그램 일일 수익률 리포트와 동일한 기준 (_daily_pnl_report)
# - 거래소 펀딩 정산 주기와도 정렬
PTP_SESSION_TZ_OFFSET_SEC = 0   # UTC 자정 = KST 09:00 — 텔레그램 일일 리셋 시각과 통일

# ★ V10.26: 쿨다운 대폭 단축 — 빠른 평단 압축으로 SL 방지
DCA_COOLDOWN_BY_TIER = {2: 0, 3: 0, 4: 0}  # ★ V10.29b: 쿨다운 전면 제거
DCA_COOLDOWN_SEC     = 0     # 레거시 호환용

# ═══════════════════════════════════════════════════════════════════
# TP / Trailing
# ═══════════════════════════════════════════════════════════════════
TP1_PCT = 1.8   # ★ v10.8: 방어형 — 빠른 확정 (레거시, 미사용)

# ★ V10.27: TP1 고정 threshold (ROI%) — worst_roi/ATR 스케일링 전부 제거
# T1~T3: 고정값. T4만 worst_roi+2.0 (plan_tp1에서 처리)
# ★ V10.29: T3/T4 TP 두배
TP1_FIXED = {1: 2.0, 2: 1.5, 3: 2.4, 4: 1.6}  # T1 스캘핑 2.0% 유지

# ★ V10.27c: HARD_SL = DCA 트리거 -1% / T4는 체결가 -2%
# ★ V10.29: SL = 다음 DCA 트리거 - 2%
HARD_SL_BY_TIER = {1: -3.8, 2: -5.6, 3: -10.0}  # ★ V10.29b: T3 스윙 -10% (손절후 65% 반전)

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
    1: 1.8,    # T1 — MR 자리, 거의 확정
    2: 1.5,    # T2 — 빠른 탈출
    3: 1.2,    # T3 — 평단 압축 탈출
    4: 0.8,    # T4 — 최종 구제, 수수료+알파
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
OPEN_CORR_MIN           = 0.60   # ★ V10.29c: 0.40→0.60 (저상관 심볼 진입 차단 — OP/ARB 손실 방지)
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


def calc_trim_qty(total_amt: float, tier: int, ep: float = 0.0, bal: float = 0.0,
                  mark_price: float = 0.0) -> float:
    """★ V10.29d: Trim 수량 — 순수 노셔널 기반 + 안전 캡.

    현재 포지션 노셔널에서 목표 tier 노셔널을 빼서 트림할 수량 계산.
    ★ 안전장치: tier 비중 기반 최대값 초과 방지 (이중 trim 방어)
    """
    if tier < 1 or total_amt <= 0:
        return 0.0

    target_tier = tier - 1
    price = mark_price if mark_price > 0 else ep

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
        return min(qty, max_trim_qty)  # ★ 안전 캡

    # fallback: 비율 방식 (bal 없을 때)
    return min(total_amt * (tier_w / cum_w_current), max_trim_qty)


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
BC_ENABLED          = True
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
BC_PULLBACK_MAX     = 0.05      # ★ V10.31b: 고점 대비 5% 이상 빠지면 스킵 (이미 회귀 진행)
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
