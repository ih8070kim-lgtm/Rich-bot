@echo off
cd /d "c:\Users\김익현\OneDrive\Desktop\부자만들기"
echo === V9 Import Test ===
python -W ignore -c "
import sys, os
sys.path.insert(0, '.')
errors = []
mods = [
    'v9.config','v9.types','v9.utils.utils_time','v9.utils.utils_math',
    'v9.logging.schemas','v9.logging.logger_csv',
    'v9.datafeed.market_snapshot','v9.datafeed.universe_asym_v2',
    'v9.risk.slot_manager','v9.risk.risk_manager',
    'v9.execution.position_book','v9.execution.order_router','v9.execution.execution_engine',
    'v9.strategy.planners','v9.strategy.strategy_core','v9.app.runner'
]
for m in mods:
    try:
        __import__(m)
        print('  [OK]', m)
    except Exception as e:
        print('  [FAIL]', m, str(e))
        errors.append(m)
print()
if errors:
    print('[FAIL]', len(errors), 'errors')
else:
    print('[ALL OK] V9 import success')
"
echo.
echo === slot_manager test ===
python -W ignore -c "
from v9.risk.slot_manager import count_slots
st = {
    'ETH/USDT': {'active': True, 'pending_entry': None, 'pending_exit': None, 'p': {'side': 'buy', 'step': 0, 'dca_level': 1}},
    'SOL/USDT': {'active': True, 'pending_entry': None, 'pending_exit': None, 'p': {'side': 'sell', 'step': 1, 'dca_level': 2}},
    'XRP/USDT': {'active': False, 'pending_entry': {'side': 'buy'}, 'pending_exit': None, 'p': None},
}
s = count_slots(st)
print('  hard_total=%d (expect 3)' % s.total)
print('  risk_total=%d (expect 2, SOL step=1 excluded)' % s.risk_total)
print('  risk_long=%d (expect 2)' % s.risk_long)
print('  risk_short=%d (expect 0)' % s.risk_short)
assert s.total == 3 and s.risk_total == 2 and s.risk_long == 2 and s.risk_short == 0
print('  [OK] slot_manager logic correct')
"
echo.
echo === KillSwitch test ===
python -W ignore -c "
import time
from v9.risk.risk_manager import evaluate_intent
from v9.types import Intent, IntentType, MarketSnapshot, RejectCode
snap = MarketSnapshot(
    tickers={}, all_prices={'ETH/USDT': 3000.0}, all_volumes={},
    ohlcv_pool={}, correlations={},
    btc_price=50000.0, btc_1h_change=0.0, btc_6h_change=0.0, dev_ma=0.0,
    real_balance_usdt=10000.0, free_balance_usdt=2000.0,
    margin_ratio=0.8,
    baseline_balance=10000.0,
    global_targets_long=[], global_targets_short=[],
    timestamp=time.time(), valid=True,
)
r1 = evaluate_intent(Intent(trace_id='t1', intent_type=IntentType.OPEN, symbol='ETH/USDT', side='buy', qty=0.1), snap, {}, {}, {'use_long': True, 'use_short': True})
r2 = evaluate_intent(Intent(trace_id='t2', intent_type=IntentType.DCA, symbol='ETH/USDT', side='buy', qty=0.1), snap, {}, {}, {'use_long': True, 'use_short': True})
assert not r1.approved and r1.reject_code == RejectCode.REJECT_KILLSWITCH_BLOCK_NEW
assert not r2.approved and r2.reject_code == RejectCode.REJECT_KILLSWITCH_BLOCK_DCA
print('  [OK] MR=0.8 OPEN ->', r1.reject_code.value)
print('  [OK] MR=0.8 DCA  ->', r2.reject_code.value)
print('  [ALL TESTS PASSED]')
"
echo.
pause
