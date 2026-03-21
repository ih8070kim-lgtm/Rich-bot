import pytest


def test_sanity_import():
    """
    가장 중요한 스모크 테스트:
    - 모듈 import만으로 크래시 나는지(순환 import, 환경변수 누락 등) 잡는다.
    - 네 봇은 '돌리기 전에' 여기서 많이 터짐.
    """
    # TODO: 네 엔트리포인트 모듈명으로 바꿔라 (예: main, run_bot 등)
    import main  # noqa: F401


def test_toggle_blocks_entries():
    """
    use_long/use_short 토글이 꺼지면 진입/추가매수/헷지까지 '물리 차단'되는지.
    구현 포인트:
    - 엔진이 '진입 의사결정 함수'를 제공해야 테스트가 쉬움
    """
    pytest.xfail("TODO: 엔진 인터페이스 연결 후 활성화")

    # 예시(너 구조에 맞게 변경):
    # from strategy import Strategy
    # s = Strategy(use_long=False, use_short=True)
    # decision = s.decide_entry(...)
    # assert decision is None


def test_slot_only_active_counts():
    """
    슬롯 기준: active=True인 포지션만 슬롯을 먹는다.
    """
    pytest.xfail("TODO: 포지션/슬롯 매니저 연결 후 활성화")

    # 예시:
    # from slot_manager import SlotManager
    # sm = SlotManager(max_long=4, max_short=4)
    # sm.add_position(symbol="BTC", side="long", active=False)
    # assert sm.used_long_slots == 0


def test_corr_filter_blocks_hedge_when_low():
    """
    corr >= 0.6 아니면 헷지 진입 금지.
    """
    pytest.xfail("TODO: hedge_engine 선택로직 연결 후 활성화")

    # 예시:
    # from hedge_engine import select_hedge_candidate
    # cand = select_hedge_candidate(corr_map={"ETH":0.55, "XRP":0.58}, min_corr=0.6)
    # assert cand is None


def test_hard_stop_triggers_at_15pct_loss():
    """
    하드스탑 -15%에서 강제 종료 트리거가 켜지는지.
    """
    pytest.xfail("TODO: risk_engine 인터페이스 연결 후 활성화")

    # 예시:
    # from risk_engine import should_hard_stop
    # assert should_hard_stop(unrealized_pnl_pct=-15.0) is True
    # assert should_hard_stop(unrealized_pnl_pct=-14.99) is False
