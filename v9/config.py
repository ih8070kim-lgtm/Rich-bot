"""
V9 Trinity Config  (v9.8 — 2026-03-09 리빌드)
===============================================
v9.7 → v9.8 변경:
  HARD_SL_BASE          -8.0%  (레버리지 기준 ROI -8%)
  HARD_SL_FACTOR_MIN    1.0 (고정)
  HARD_SL_FACTOR_MAX    1.0 (고정)
  HARD_SL_ATR_BASE      0.003 → 0.0015 (plan_open ATR base와 통일)
  Pressure Relief       제거 (Section 7 미구현)
  ASYM_FALLBACK_MR90    제거 (MR≥0.9 구간 OPEN intent 생성 자체 차단)
"""

# ═══════════════════════════════════════════════════════════════════
# 슬롯 설정
# ═══════════════════════════════════════════════════════════════════
TOTAL_MAX_SLOTS = 10   # ★ v10.15: 양방향 합산 (한쪽 5 × 2)
MAX_LONG        = 5    # ★ v10.15: 한쪽 5 (MR+HEDGE 합산)
MAX_SHORT       = 5
MAX_MR_PER_SIDE = 4    # ★ v10.15: MR은 방향당 최대 4
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
KILLSWITCH_BLOCK_ALL_MR  = 0.8   # MR≥0.8 → OPEN/DCA 금지
KILLSWITCH_BLOCK_NEW_MR  = 0.7   # MR≥0.7 → OPEN 금지

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
# DCA 거리 (전 레짐 통일)
# ═══════════════════════════════════════════════════════════════════
DCA_DISTANCES = {
    "LOW":       [0.010, 0.008, 0.018],   # ★ v10.5: T2=1.0% / T3=0.8% (평단 조기 압축)
    "MID":       [0.010, 0.008, 0.018],
    "HIGH_UP":   [0.012, 0.025, 0.036],
    "HIGH_DOWN": [0.012, 0.025, 0.036],
    "HIGH":      [0.012, 0.025, 0.036],
}

# ═══════════════════════════════════════════════════════════════════
# DCA 비중  T1:T2:T3:T4 = 45:40:13:2
#   T1/T2 중심 평균회귀 | T3 보조 복구 | T4 비대칭 슬롯 트리거
#   누적 노출: T1=45 / T2=85 / T3=98 / T4=100  |  avg_ep ≈ -0.75%
# ═══════════════════════════════════════════════════════════════════
# ★ v10.8: 5단 DCA — 완만한 마진 증가 곡선
# T1:T2:T3:T4:T5 = 20:15:20:25:20  |  누적: 20/35/55/80/100
# ★ v10.12: T1/T2↑ T3/T4↓ (승률 높은 구간에 자본 집중)
# 로그 분석: T1(85%승률) T2(72%) → 힘 실기, T3(42%) T4(67%) → 리스크 축소
DCA_WEIGHTS = [20, 20, 20, 20, 20]  # ★ 최종 아키텍처: equal weight (bt_source 우승안)

DCA_LIMIT_TIMEOUT_SEC = 60
DCA_MIN_CORR          = 0.5

# ★ v10.8: 5단 쿨다운
DCA_COOLDOWN_BY_TIER = {2: 1800, 3: 1800, 4: 2700, 5: 3600}  # ★ 최종: DCA간격 -8.25% 통일
DCA_COOLDOWN_SEC     = 600   # 레거시 호환용 (dca_engine 등 참조 시 사용)

# ═══════════════════════════════════════════════════════════════════
# TP / Trailing
# ═══════════════════════════════════════════════════════════════════
TP1_PCT = 1.8   # ★ v10.8: 방어형 — 빠른 확정

# ★ v10.14c: min_roi 반등 α — worst_roi + α ≥ current_roi → TP1
# T1/T2: 승률 높으므로 넉넉히 (기존 고정TP와 유사)
# T3~T5: DCA 깊은 구간, 빠른 탈출 (바닥 대비 1.5% 반등)
REBOUND_ALPHA = {1: 10.0, 2: 10.0, 3: 7.5, 4: 7.5, 5: 7.5}  # ★ base×width×tp1_mult (bt_source 기준)

# ★ v10.8: DCA 깊을수록 빨리 탈출 (레거시, TP1_PREORDER fallback용)
TP1_PCT_BY_DCA = {
    1: 1.8,    # T1 — MR 자리, 거의 확정
    2: 1.5,    # T2 — 빠른 탈출
    3: 1.2,    # T3 — 평단 압축 탈출
    4: 0.8,    # T4 — 수수료+알파
    5: 0.5,    # T5 — 탈출만
}
TP_ATR_POWER    = 0.3
TP_ATR_MIN_MULT = 0.7
TP_ATR_MAX_MULT = 1.5

TP1_PARTIAL_RATIO  = 0.40
TP2_PCT            = 4.0
TP2_PARTIAL_RATIO  = 0.30
TRAILING_TIMEOUT_MIN = 45

# ═══════════════════════════════════════════════════════════════════
# OPEN / CORR
# ═══════════════════════════════════════════════════════════════════
HEDGE_OPEN_CORR_MIN     = 0.6
OPEN_CORR_MIN           = 0.50   # ★ PATCH: 진입 상관성 하한 (config 통합)
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

# ★ v10.15: 롱/숏 분리 파라미터  ★ PATCH: 상관성 0.50 통일 (숏 차단 해소)
LONG_MIN_CORR   = 0.50
LONG_BETA_MIN   = 0.80
LONG_BETA_MAX   = 2.00
SHORT_MIN_CORR  = 0.50
SHORT_BETA_MIN  = 0.70
SHORT_BETA_MAX  = 2.00

# ★ v10.15: HIGH 레짐 sticky
HIGH_STICKY_SEC = 300   # HIGH 진입 후 5분간 유지

# ★ v10.15: MinROI JSON 상태 파일
MINROI_FILE     = "v9_minroi.json"
RECONCILE_INTERVAL_SEC = 45   # 바이낸스 대조 주기 (현재 미사용, 매틱 sync)

# ★ v10.15: Skew MR 추가 진입
SKEW_HEDGE_TRIGGER = 0.12   # ★ PATCH: 15%→12% (시장가 전환 + calc_skew 헷지 제외로 안정화)

# ═══════════════════════════════════════════════════════════════════
# TP Lock — 마진 불균형 시 light side 익절 잠금
# ═══════════════════════════════════════════════════════════════════
TP_LOCK_SKEW_1       = 0.10    # skew 10%p → 1개 잠금
TP_LOCK_SKEW_2       = 0.20    # skew 20%p → 2개 잠금
TP_LOCK_RELEASE      = 0.07    # 7%p 이하 → 해제 (히스테리시스)
TP_LOCK_STRESS_ROI   = -4.0    # heavy side 이 ROI 이하 → 스트레스 가속
TP_LOCK_STRESS_MULT  = 0.80    # 스트레스 시 트리거 ×0.80
TP_LOCK_MIN_ROI      = 2.0     # 잠금 대상 최소 ROI (이하 제외)
TP_LOCK_EXIT_ROI     = 1.0     # 수익 소진 시 강제 해제

# ─── v10.17: 스큐 에스컬레이션 / Heavy side 조기 TP ────────────────────────
SKEW_STAGE2_TRIGGER     = 0.15  # 2단계: TP Lock 2개 + 헷지 필요조건 체크 시작
SKEW_HEAVY_TP_ROI_1     = 1.5   # stage1 (10~15%) heavy side 조기 TP ROI 기준
SKEW_HEAVY_TP_ROI_2     = 1.0   # stage2 (≥15%) heavy side 조기 TP ROI 기준
SKEW_STAGE2_TIMEOUT_SEC = 900   # 15분: stage2 지속 → 헷지 필요조건③ 충족
SKEW_HEDGE_STRESS_ROI   = -3.0  # heavy side 이 ROI 이하 → 헷지 DCA/ROI 조건 완화

GLOBAL_BLACKLIST = [
    "BTC/USDT", "DOGE/USDT", "SHIB/USDT", "PEPE/USDT",
    "FLOKI/USDT", "BONK/USDT", "WIF/USDT", "1000PEPE/USDT",
    "LUNC/USDT", "USTC/USDT", "FTM/USDT", "SNX/USDT",
]

# ★ v10.5: 심볼별 방향 바이어스
# LONG_ONLY  — 숏 진입 금지 (대형주 / 회복력 강함)
# SHORT_ONLY — 롱 진입 금지 (소형 알트 / 하락 드리프트)
# NEUTRAL    — 양방향 허용
LONG_ONLY_SYMBOLS = {
    "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT",
    "AVAX/USDT", "TRX/USDT", "TON/USDT", "DOT/USDT", "LINK/USDT",
    "ICP/USDT", "ETC/USDT", "XLM/USDT",
}

SHORT_ONLY_SYMBOLS = {
    "ARB/USDT", "OP/USDT", "STRK/USDT", "TIA/USDT", "SEI/USDT",
    "INJ/USDT", "RUNE/USDT", "FET/USDT", "RNDR/USDT",
    "AGIX/USDT", "AKT/USDT", "WLD/USDT", "GRT/USDT", "FIL/USDT",
}

NEUTRAL_SYMBOLS = {
    "SUI/USDT", "APT/USDT", "NEAR/USDT", "ATOM/USDT",
    "AAVE/USDT", "UNI/USDT", "STX/USDT", "MATIC/USDT", "EOS/USDT",
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
LOG_SKEW_FILE      = "log_skew.csv"      # ★ v10.17: 스큐 모니터링
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
# Falling Knife Filter  (★ v9.9 신규)
# ═══════════════════════════════════════════════════════════════════
FALLING_KNIFE_BARS      = 3      # 최근 N개 5m 봉
FALLING_KNIFE_THRESHOLD = 0.020  # ★ v10.9: 1.2% → 2.0% (MR 진입 차단 완화)

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
