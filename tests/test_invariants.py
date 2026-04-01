"""
Trinity Bot — Invariant Tests
==============================
거래소/네트워크 연결 없이 핵심 로직의 불변식을 검증.

fixture는 tests/conftest.py 참고.
"""

import pytest


# ═══════════════════════════════════════════════════════════════
# 1. Smoke Test
# ═══════════════════════════════════════════════════════════════

def test_sanity_import():
    """
    순환 import, 환경변수 누락, 문법 오류 등으로 인한 크래시 감지.
    봇 시작 전 가장 먼저 걸러야 할 오류들.
    """
    import v9.config          # noqa: F401
    import v9.types           # noqa: F401
    import v9.engines.hedge_core   # noqa: F401
    import v9.strategy.planners    # noqa: F401
    import v9.risk.slot_manager    # noqa: F401
    import v9.logging.schemas      # noqa: F401
    import v9.logging.logger_csv   # noqa: F401


# ═══════════════════════════════════════════════════════════════
# 2. Config 상수 범위 검증
# ═══════════════════════════════════════════════════════════════

class TestConfigConstants:
    """설정값이 논리적으로 말이 되는지 확인 (실수로 값이 뒤집히는 것 방지)."""

    def test_skew_stage2_trigger_positive(self):
        """Stage2 트리거는 양수여야 한다."""
        from v9.config import SKEW_STAGE2_TRIGGER
        assert SKEW_STAGE2_TRIGGER > 0

    def test_stress_roi_is_negative(self):
        """스트레스 ROI 기준은 음수여야 한다."""
        from v9.config import SKEW_HEDGE_STRESS_ROI
        assert SKEW_HEDGE_STRESS_ROI < 0

    def test_dca_weights_4_tiers(self):
        """★ V10.22: DCA 4단 weight 검증."""
        from v9.config import DCA_WEIGHTS
        assert len(DCA_WEIGHTS) == 4
        assert sum(DCA_WEIGHTS) == 100

    def test_leverage_positive(self):
        """레버리지는 양수여야 한다."""
        from v9.config import LEVERAGE
        assert LEVERAGE > 0

    def test_max_slots_positive(self):
        """MAX 슬롯은 양수."""
        from v9.config import MAX_LONG, MAX_SHORT, TOTAL_MAX_SLOTS
        assert MAX_LONG > 0
        assert MAX_SHORT > 0
        assert TOTAL_MAX_SLOTS >= MAX_LONG + MAX_SHORT


# ═══════════════════════════════════════════════════════════════
# 3. calc_skew 검증
# ═══════════════════════════════════════════════════════════════

class TestCalcSkew:
    """calc_skew(st, total_cap) → (skew, long_mr, short_mr) 검증."""

    def test_balanced_returns_zero_skew(self, balanced_st):
        """롱=숏 마진이면 skew=0."""
        from v9.engines.hedge_core import calc_skew
        skew, long_m, short_m = calc_skew(balanced_st, 10_000.0)
        assert abs(skew) < 1e-9
        assert abs(long_m - short_m) < 1e-9

    def test_long_heavy_skew(self, long_heavy_st):
        """롱이 더 크면 skew > 0, long_mr > short_mr."""
        from v9.engines.hedge_core import calc_skew
        st, snap, total_cap = long_heavy_st
        skew, long_m, short_m = calc_skew(st, total_cap)
        assert skew > 0
        assert long_m > short_m
        # 예상값 근사: long=0.30, short=0.10 → skew≈0.20
        assert abs(skew - 0.20) < 0.01

    def test_short_heavy_skew(self, short_heavy_st):
        """숏이 더 크면 skew > 0, short_mr > long_mr."""
        from v9.engines.hedge_core import calc_skew
        st, snap, total_cap = short_heavy_st
        skew, long_m, short_m = calc_skew(st, total_cap)
        assert skew > 0
        assert short_m > long_m

    def test_zero_total_cap(self, balanced_st):
        """total_cap=0 이면 (0,0,0) 반환해야 ZeroDivisionError 없음."""
        from v9.engines.hedge_core import calc_skew
        result = calc_skew(balanced_st, 0.0)
        assert result == (0.0, 0.0, 0.0)

    def test_hedge_role_excluded(self, snap_factory, pos, sym_st):
        """CORE_HEDGE 포지션은 skew 계산에서 제외돼야 한다."""
        from v9.engines.hedge_core import calc_skew
        # 순수 long 포지션 하나 + CORE_HEDGE short 포지션 하나
        # CORE_HEDGE를 제외하면 long_mr > 0, short_mr = 0 → skew > 0
        # CORE_HEDGE를 포함하면 skew가 줄어들어야 하지만, 여기서는 "제외 검증"
        st = {
            "ETH/USDT": sym_st(
                p_long=pos(ep=100.0, amt=300.0, side="buy", role="CORE_MR"),
                p_short=pos(ep=100.0, amt=300.0, side="sell", role="CORE_HEDGE"),
            )
        }
        total_cap = 10_000.0
        skew, long_m, short_m = calc_skew(st, total_cap)
        # CORE_HEDGE short 제외 → short_mr = 0, long_mr > 0
        assert long_m > 0
        assert short_m == 0.0
        assert skew == long_m

    def test_empty_st(self):
        """빈 state → skew=0."""
        from v9.engines.hedge_core import calc_skew
        skew, long_m, short_m = calc_skew({}, 10_000.0)
        assert skew == 0.0


# ═══════════════════════════════════════════════════════════════
# 4. Skew-Aware TP (_skew_tp_adjustment) 검증  — ★ V10.22
# ═══════════════════════════════════════════════════════════════

class TestSkewTpAdjustment:
    """_skew_tp_adjustment(pos_side, st, snapshot) → dict 검증."""

    def test_no_skew_returns_neutral(self, snap_factory, pos, sym_st):
        """skew < 5% 면 mult=1.0, blocked=False."""
        from v9.strategy.planners import _skew_tp_adjustment
        total_cap = 10_000.0
        snap = snap_factory(real_balance_usdt=total_cap)
        st = {
            "ETH/USDT": sym_st(
                p_long=pos(ep=100.0, amt=33.0, side="buy"),
                p_short=pos(ep=100.0, amt=27.0, side="sell"),
            )
        }
        result = _skew_tp_adjustment("buy", st, snap)
        assert result["skew_mult"] == 1.0
        assert result["blocked"] is False
        assert result["full_close"] is False

    def test_heavy_side_lower_mult(self, snap_factory, pos, sym_st):
        """skew 5~10%: heavy side mult < 1.0, light side mult > 1.0."""
        from v9.strategy.planners import _skew_tp_adjustment
        total_cap = 10_000.0
        snap = snap_factory(real_balance_usdt=total_cap)
        # long heavy: 40*100/3≈1333 → 0.133, short: 20*100/3≈667 → 0.067
        # skew ≈ 0.067 (5~10% 구간)
        st = {
            "ETH/USDT": sym_st(
                p_long=pos(ep=100.0, amt=40.0, side="buy"),
                p_short=pos(ep=100.0, amt=20.0, side="sell"),
            )
        }
        heavy = _skew_tp_adjustment("buy", st, snap)
        light = _skew_tp_adjustment("sell", st, snap)
        assert heavy["skew_mult"] < 1.0
        assert light["skew_mult"] > 1.0

    def test_high_skew_blocks_light(self, snap_factory, pos, sym_st):
        """skew ≥ 15%: light side 차단, heavy side 풀클로즈."""
        from v9.strategy.planners import _skew_tp_adjustment
        total_cap = 10_000.0
        snap = snap_factory(real_balance_usdt=total_cap)
        # long heavy: 80*100/3≈2667 → 0.267, short: 20*100/3≈667 → 0.067
        # skew ≈ 0.20 (>15%)
        st = {
            "ETH/USDT": sym_st(
                p_long=pos(ep=100.0, amt=80.0, side="buy"),
                p_short=pos(ep=100.0, amt=20.0, side="sell"),
            )
        }
        heavy = _skew_tp_adjustment("buy", st, snap)
        light = _skew_tp_adjustment("sell", st, snap)
        assert heavy["full_close"] is True
        assert heavy["skew_mult"] <= 0.3
        assert light["blocked"] is True


# ═══════════════════════════════════════════════════════════════
# 5. 슬롯 잔고 규칙 검증
# ═══════════════════════════════════════════════════════════════

class TestSlotBalance:
    """Slot Balance Rule A: opposite=0 AND current>=3 → 진입/DCA 차단."""

    def test_count_slots_long_only(self, pos, sym_st):
        """롱 포지션만 있을 때 슬롯 카운트."""
        from v9.risk.slot_manager import count_slots
        st = {
            "ETH/USDT": sym_st(p_long=pos(ep=100.0, amt=1.0, side="buy")),
            "BTC/USDT": sym_st(p_long=pos(ep=100.0, amt=1.0, side="buy")),
        }
        counts = count_slots(st)
        assert counts.long == 2
        assert counts.short == 0
        assert counts.total == 2

    def test_count_slots_mixed(self, pos, sym_st):
        """롱+숏 혼합."""
        from v9.risk.slot_manager import count_slots
        st = {
            "ETH/USDT": sym_st(
                p_long=pos(ep=100.0, amt=1.0, side="buy"),
                p_short=pos(ep=100.0, amt=1.0, side="sell"),
            ),
            "BTC/USDT": sym_st(p_short=pos(ep=100.0, amt=1.0, side="sell")),
        }
        counts = count_slots(st)
        assert counts.long == 1
        assert counts.short == 2
        assert counts.total == 3

    def test_empty_st_zero_slots(self):
        """빈 state → 모든 슬롯 0."""
        from v9.risk.slot_manager import count_slots
        counts = count_slots({})
        assert counts.total == 0
        assert counts.long == 0
        assert counts.short == 0


# ═══════════════════════════════════════════════════════════════
# 6. 헷지 필요조건 검증 (_is_hedge_required)
# ═══════════════════════════════════════════════════════════════

class TestIsHedgeRequired:
    """_is_hedge_required: MR ≥ 0.7 이면 즉시 True."""

    def _reset_stage2(self):
        import v9.engines.hedge_core as hc
        hc._skew_stage2_enter_ts = 0.0

    def test_mr_above_07_triggers_hedge(self, snap_factory):
        """MR ≥ 0.7 → 헷지 필요."""
        from v9.engines.hedge_core import _is_hedge_required
        self._reset_stage2()
        snap = snap_factory(margin_ratio=0.75)
        result = _is_hedge_required({}, snap, skew=0.20, heavy_side="buy")
        assert result is True

    def test_mr_below_07_no_hedge(self, snap_factory):
        """MR < 0.7, 모든 heavy 슬롯 ROI > 0 → 헷지 불필요."""
        from v9.engines.hedge_core import _is_hedge_required
        self._reset_stage2()
        snap = snap_factory(
            margin_ratio=0.30,
            all_prices={"ETH/USDT": 110.0},  # ep=100 → ROI > 0
        )
        from tests.conftest import _pos, _sym_st
        st = {"ETH/USDT": _sym_st(p_long=_pos(ep=100.0, amt=1.0, side="buy"))}
        result = _is_hedge_required(st, snap, skew=0.20, heavy_side="buy")
        assert result is False

    def test_all_heavy_negative_roi_triggers_hedge(self, snap_factory, pos, sym_st):
        """heavy side 전 슬롯 ROI < 0 → 헷지 필요."""
        from v9.engines.hedge_core import _is_hedge_required
        self._reset_stage2()
        # ep=100, current price=80 → ROI < 0
        snap = snap_factory(
            margin_ratio=0.20,
            all_prices={"ETH/USDT": 80.0},
        )
        st = {"ETH/USDT": sym_st(p_long=pos(ep=100.0, amt=1.0, side="buy"))}
        result = _is_hedge_required(st, snap, skew=0.20, heavy_side="buy")
        assert result is True
