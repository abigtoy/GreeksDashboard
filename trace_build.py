import sys, json, subprocess
sys.path.insert(0, 'dashboard_v2')

from risk_engine import build_tree
from api_server import _load_yesterday_snapshot

# 获取当前数据
r = subprocess.run(['curl', '-s', 'http://localhost:5000/api/_diag'], capture_output=True, text=True)
# The _diag doesn't return direct data, use another approach

# Get ticks and positions from shared state via a direct call
from api_server import _snapshot, _shared_state, _shared_lock

snap = _snapshot()
positions = snap['positions']
ticks_raw = snap.get('ticks', {})
contracts = snap['contracts']
settlement_dict = snap['settlement_dict']
settlement_prices = snap['settlement_prices']
tree_obj = snap['tree']

print('=== Raw snapshot data ===')
print('positions count:', len(positions))
print('contracts count:', len(contracts))
print('ticks type:', type(ticks_raw))
print('settlement_dict count:', len(settlement_dict))
print('settlement_prices count:', len(settlement_prices))
print()

# Get yesterday_snapshot
yesterday = _load_yesterday_snapshot()
print('yesterday_snapshot count:', len(yesterday))

# Check SC keys
sc_keys = sorted([k for k in yesterday if 'sc' in k.lower()])
print('SC keys in yesterday_snapshot:', sc_keys)

# Build tree fresh
from collections import defaultdict
product_map = defaultdict(lambda: defaultdict(list))
for pos in positions:
    sym = pos.get('symbol', '').split('.')[0]
    product = 'SC'
    expiry = '2611'
    product_map[product][expiry].append(pos)

print()
print('SC positions in product_map:')
for month, pos_list in product_map['SC'].items():
    for pos in pos_list:
        print('  ', pos)
