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

    def test_tp_lock_skew_order(self):
        """TP Lock 스큐 트리거: SKEW_1 < SKEW_2 < SKEW_3."""
        from v9.config import TP_LOCK_SKEW_1, TP_LOCK_SKEW_2, TP_LOCK_SKEW_3
        assert TP_LOCK_SKEW_1 < TP_LOCK_SKEW_2 < TP_LOCK_SKEW_3

    def test_tp_lock_release_below_trigger(self):
        """해제 임계값은 발동 임계값보다 낮아야 히스테리시스가 작동한다."""
        from v9.config import TP_LOCK_SKEW_1, TP_LOCK_RELEASE
        assert TP_LOCK_RELEASE < TP_LOCK_SKEW_1

    def test_skew_stage2_above_stage1(self):
        """Stage2 트리거는 Stage1(TP_LOCK_SKEW_1)보다 커야 한다."""
        from v9.config import TP_LOCK_SKEW_1, SKEW_STAGE2_TRIGGER
        assert SKEW_STAGE2_TRIGGER > TP_LOCK_SKEW_1

    def test_heavy_tp_roi_stage2_below_stage1(self):
        """Stage2(위기) TP ROI 기준이 Stage1보다 낮아야 더 빨리 익절한다."""
        from v9.config import SKEW_HEAVY_TP_ROI_1, SKEW_HEAVY_TP_ROI_2
        assert SKEW_HEAVY_TP_ROI_2 < SKEW_HEAVY_TP_ROI_1

    def test_stress_roi_is_negative(self):
        """스트레스 ROI 기준은 음수여야 한다."""
        from v9.config import TP_LOCK_STRESS_ROI, SKEW_HEDGE_STRESS_ROI
        assert TP_LOCK_STRESS_ROI < 0
        assert SKEW_HEDGE_STRESS_ROI < 0

    def test_stress_mult_reduces_threshold(self):
        """스트레스 배수가 1 미만이어야 트리거를 낮춘다 (더 빨리 발동)."""
        from v9.config import TP_LOCK_STRESS_MULT
        assert 0 < TP_LOCK_STRESS_MULT < 1.0

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
# 4. TP Lock (_calc_tp_lock) 검증
# ═══════════════════════════════════════════════════════════════

class TestCalcTpLock:
    """_calc_tp_lock(snapshot, st) → (locked: set, heavy_side: str, skew: float)."""

    def _reset_tp_lock(self):
        """모듈 레벨 상태 초기화 (테스트 격리)."""
        import v9.strategy.planners as pl
        pl._tp_lock_active = False
        pl._heavy_tp_logged = set()

    def test_no_lock_when_skew_below_threshold(self, snap_factory, pos, sym_st):
        """skew < 10% 면 잠금 없음."""
        from v9.strategy.planners import _calc_tp_lock
        self._reset_tp_lock()
        # skew ≈ 0.05 (5%)
        total_cap = 10_000.0
        snap = snap_factory(real_balance_usdt=total_cap)
        # long: 150*100/3 = 5000 → 0.50, short: 100*100/3 = 3333 → 0.33 → skew≈0.17
        # 실제로 낮은 skew를 만들려면 균형에 가깝게
        st = {
            "ETH/USDT": sym_st(
                p_long=pos(ep=100.0, amt=33.0, side="buy"),   # 33*100/3=1100 → 0.11
                p_short=pos(ep=100.0, amt=27.0, side="sell"),  # 27*100/3= 900 → 0.09
            )
        }
        locked, heavy_side, skew = _calc_tp_lock(snap, st)
        # skew ≈ 0.02 < 0.10 → 잠금 없음
        assert len(locked) == 0
        assert skew < 0.10

    def test_lock_1_at_stage1(self, snap_factory, pos, sym_st):
        """skew 10~15% → lock_count=1, light side 상위 1개 잠금.
        안전장치: light_total >= lock_count+1 필요 → short 2개 세팅.
        """
        from v9.strategy.planners import _calc_tp_lock
        self._reset_tp_lock()
        total_cap = 50_000.0
        # short(sell) ep=100, cp=80 → ROI = (100-80)/100*3*100 = +60% > TP_LOCK_MIN_ROI
        snap = snap_factory(
            real_balance_usdt=total_cap,
            all_prices={"ETH/USDT": 100.0, "BTC/USDT": 80.0, "SOL/USDT": 80.0},
        )
        # long: 451*100/3≈15033 → 0.3007 (>0.10 초과)
        # short: 150*100/3*2=10000 → 0.20  → skew≈0.10
        st = {
            "ETH/USDT": sym_st(p_long=pos(ep=100.0, amt=451.0, side="buy")),
            "BTC/USDT": sym_st(p_short=pos(ep=100.0, amt=150.0, side="sell")),
            "SOL/USDT": sym_st(p_short=pos(ep=100.0, amt=150.0, side="sell")),
        }
        locked, heavy_side, skew = _calc_tp_lock(snap, st)
        assert skew >= 0.10
        assert heavy_side == "buy"
        # 안전장치: min(1, max(0, 2-1)) = 1
        assert len(locked) == 1

    def test_lock_2_at_stage2(self, snap_factory, pos, sym_st):
        """skew ≥ 15% → lock_count=2.
        안전장치: light_total >= 3 필요 → short 3개 세팅.
        """
        from v9.strategy.planners import _calc_tp_lock
        self._reset_tp_lock()
        total_cap = 50_000.0
        # short(sell) ep=100, cp=80 → ROI=+60%
        snap = snap_factory(
            real_balance_usdt=total_cap,
            all_prices={"ETH/USDT": 100.0, "BTC/USDT": 100.0,
                        "SOL/USDT": 80.0, "XRP/USDT": 80.0, "ADA/USDT": 80.0},
        )
        # long: 600*100/3=20000 → 0.40
        # short: 150*100/3*3=15000 → 0.30 → skew=0.10 (stage1)
        # 더 많은 long으로 skew 키우기: long 450+300=750
        # long: 750*100/3=25000 → 0.50, short: 150*100/3*3=15000 → 0.30 → skew=0.20
        st = {
            "ETH/USDT": sym_st(p_long=pos(ep=100.0, amt=450.0, side="buy")),
            "BTC/USDT": sym_st(p_long=pos(ep=100.0, amt=300.0, side="buy")),
            "SOL/USDT": sym_st(p_short=pos(ep=100.0, amt=150.0, side="sell")),
            "XRP/USDT": sym_st(p_short=pos(ep=100.0, amt=150.0, side="sell")),
            "ADA/USDT": sym_st(p_short=pos(ep=100.0, amt=150.0, side="sell")),
        }
        locked, heavy_side, skew = _calc_tp_lock(snap, st)
        assert skew >= 0.15
        # 안전장치: min(2, max(0, 3-1)) = 2
        assert len(locked) == 2

    def test_heavy_side_correctly_identified(self, snap_factory, pos, sym_st):
        """heavy_side는 마진 비율이 더 큰 쪽이어야 한다."""
        from v9.strategy.planners import _calc_tp_lock
        self._reset_tp_lock()
        total_cap = 50_000.0
        snap = snap_factory(
            real_balance_usdt=total_cap,
            all_prices={"ETH/USDT": 100.0},
        )
        st = {
            "ETH/USDT": sym_st(
                p_long=pos(ep=100.0, amt=450.0, side="buy"),   # heavy
                p_short=pos(ep=100.0, amt=150.0, side="sell"),
            )
        }
        _, heavy_side, _ = _calc_tp_lock(snap, st)
        assert heavy_side == "buy"

    def test_hysteresis_release(self, snap_factory, pos, sym_st):
        """잠금 활성 후 skew가 RELEASE 미만으로 떨어지면 해제."""
        import v9.strategy.planners as pl
        from v9.strategy.planners import _calc_tp_lock
        from v9.config import TP_LOCK_RELEASE

        # 먼저 잠금 활성화
        pl._tp_lock_active = True
        pl._heavy_tp_logged = set()

        total_cap = 10_000.0
        # skew ≈ 0.02 < RELEASE(0.07) → 해제
        snap = snap_factory(real_balance_usdt=total_cap)
        st = {
            "ETH/USDT": sym_st(
                p_long=pos(ep=100.0, amt=33.0, side="buy"),
                p_short=pos(ep=100.0, amt=27.0, side="sell"),
            )
        }
        locked, heavy_side, skew = _calc_tp_lock(snap, st)
        assert skew < TP_LOCK_RELEASE
        assert len(locked) == 0
        assert pl._tp_lock_active is False


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
