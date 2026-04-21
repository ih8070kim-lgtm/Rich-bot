"""
V9 Logging Schemas
CSV 로그 컬럼 정의 (6종)
"""

# ── log_intents ─────────────────────────────────────────────────
INTENTS_COLUMNS = [
    "time", "trace_id", "intent_type", "symbol", "side",
    "qty", "price", "reason", "approved", "reject_code",
    "role", "source_sym",
]

# ── log_risk ────────────────────────────────────────────────────
RISK_COLUMNS = [
    "time", "trace_id", "symbol", "intent_type",
    "reject_code", "margin_ratio", "risk_slots_total",
    "risk_slots_long", "risk_slots_short",
    "step", "dca_level", "note"
]

# ── log_orders ──────────────────────────────────────────────────
ORDERS_COLUMNS = [
    "time", "trace_id", "symbol", "side", "order_type",
    "qty", "price", "tag", "order_id", "status"
]

# ── log_fills ───────────────────────────────────────────────────
FILLS_COLUMNS = [
    "time", "trace_id", "symbol", "side",
    "avg_price", "filled_qty", "notional", "tag", "order_id",
    # ★ 청산 체결 시 추가 (OPEN은 빈값)
    "ep", "pnl_usdt", "roi_pct", "dca_level", "hold_sec",
]

# ── log_positions ───────────────────────────────────────────────
POSITIONS_COLUMNS = [
    "time", "trace_id", "symbol", "side", "ep", "amt",
    "dca_level", "step", "roi_pct", "max_roi_seen",
    "trailing_on", "hedge_mode", "tag",
    # ★ 추가 필드
    "curr_price", "notional", "unrealized_pnl",
    # ★ v10.2: role 분류
    "role", "source_sym",
]

# ── log_trades (신규) ────────────────────────────────────────────
# OPEN→CLOSE 완성 거래 1건 = 1행. 분석의 핵심 로그
TRADES_COLUMNS = [
    "time",          # 청산 시각
    "trace_id",      # 청산 trace_id
    "symbol",
    "side",          # buy/sell (OPEN 기준)
    "ep",            # 평단
    "exit_price",    # 청산가
    "amt",           # 청산 수량
    "pnl_usdt",      # 실현 손익 ($) — 수수료 차감 전
    "roi_pct",       # 레버리지 포함 ROI%
    "dca_level",     # 최종 DCA 레벨 (T1~T4)
    "hold_sec",      # 보유 시간 (초)
    "reason",        # TRAIL_ON / TP1 / CLOSE / FORCE_CLOSE
    "hedge_mode",    # 헷지 포지션 여부
    "was_hedge",     # 헷지→노말 전환 후 청산 여부
    "max_roi_seen",  # 보유 중 최대 ROI
    "entry_type",    # ★ v9.9: MR | PULLBACK | ASYM
    # ★ v10.2: role 분류
    "role",          # CORE_MR | CORE_PULLBACK | BALANCE | HEDGE
    "source_sym",    # HEDGE일 때 소스 심볼
    # ★ V10.31d: 수수료 (맨 뒤 추가 — 기존 파싱 인덱스 유지)
    "fee_usdt",      # 청산 거래 수수료 합계 ($)
    # ★ V10.31e: T1 → T2 DCA 직전의 max_roi 보존 (측정 인프라)
    # 기존 max_roi_seen은 DCA 시 0 리셋되어 "DCA 이후 구간의 max"만 기록.
    # 이 컬럼은 T1 시점에 얼마나 반등 찍었는지 추적해 조기 익절 시나리오 검증용.
    "t1_max_roi_pre_dca",
    # ★ V10.31j: 디펜스 모드 임계 튜닝을 위한 worst_roi 보존
    # 최종 tier에서 얼마나 깊게 물렸는지 추적. T2_DEF/T3_DEF_M5 임계 재조정 근거.
    # DCA 체결 시 worst_roi=0 리셋되므로 "최종 tier 구간의 worst"만 기록.
    "worst_roi_seen",
]

# ── log_funding (★ V10.31d: 펀딩비 별도 로깅) ────────────────────
# fetch_funding_history로 주기 수집. 심볼별 펀딩 이벤트 1건 = 1행
FUNDING_COLUMNS = [
    "time",            # 펀딩 정산 시각 (거래소 기준, ISO)
    "symbol",
    "funding_usdt",    # 정산 금액 ($). 음수면 지불, 양수면 수취
    "funding_rate",    # 당시 펀딩 비율 (decimal)
    "position_amt",    # 정산 시점 포지션 수량 (부호=방향)
]

# ── log_skew (★ V10.31c: 제거됨 — 스큐 로직 V10.30에서 전면 삭제)
# SKEW_COLUMNS는 더 이상 사용되지 않음.

# ── log_universe ────────────────────────────────────────────────
UNIVERSE_COLUMNS = [
    "time", "trace_id", "top10", "long_4", "short_4",
    "regime", "btc_price", "note"
]

# ── log_hedge_sim (★ V10.31e-6: TREND vs 가상 MR 헷지 쌍 추적) ────
# MR 시그널 발생 → TREND가 실제 발사한 시점마다:
#   (1) 실제 TREND 포지션 (이미 log_trades에 기록됨)
#   (2) 가상 MR 헷지 (MR 시그널 심볼에 반대 방향, TREND와 동일 notional)
# 중간형 시뮬: DCA 트리거 + 평단 압축. TP1 2% / HARD_SL -10% 도달 시 종료.
# 목적: "TREND 실제 vs MR 가상 헷지" 병렬 PnL 비교 → 역방향 헷지 전략 유효성 검증.
HEDGE_SIM_COLUMNS = [
    "time",                   # 종료 시각
    "mr_sym",                 # MR 시그널 심볼
    "mr_side",                # MR 시그널 원 방향 (buy/sell)
    "sim_side",                # 가상 헷지 방향 (= mr_side 반대 = TREND 방향과 같음)
    "trend_sym",              # 실제 TREND가 진입한 심볼 (참조용)
    "trend_side",             # 실제 TREND 방향
    "sim_t1_ep",              # 가상 T1 진입가
    "sim_final_ep",           # 가상 최종 평단 (DCA 후 blended)
    "sim_final_tier",         # 가상 최종 tier (1/2/3)
    "sim_notional_t1",        # 가상 T1 노셔널 ($, TREND와 동일)
    "sim_final_roi",          # 가상 종료 시 ROI (%)
    "sim_max_roi",             # 가상 구간 내 최고 ROI (%)
    "sim_close_reason",       # VIRTUAL_TP1 / VIRTUAL_HARD_SL / ACTUAL_TREND_CLOSE
    "hold_sec",
]
