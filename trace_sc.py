import json, subprocess, sys
sys.path.insert(0, 'dashboard_v2')

r = subprocess.run(['curl', '-s', 'http://localhost:5000/api/dashboard'], capture_output=True, text=True)
d = json.loads(r.stdout)

def find_leaves(nodes):
    leaves = []
    for n in nodes:
        if not n.get('children'):
            leaves.append(n)
        if n.get('children'):
            leaves.extend(find_leaves(n['children']))
    return leaves

leaves = find_leaves(d.get('tree', []))
sc_leaves = [l for l in leaves if l.get('symbol','').startswith('SC')]

print('SC leaves count: ' + str(len(sc_leaves)))
for n in sorted(sc_leaves, key=lambda x: x.get('symbol','')):
    sym = n.get('symbol','')
    vol = n.get('volume')
    adj = n.get('adjust_price')
    last = n.get('last_price')
    direction = n.get('direction','')
    open_p = n.get('open_price')
    m = n.get('metrics', {})
    print('  ' + sym + ': vol=' + str(vol) + ', adj=' + str(adj) + ', last=' + str(last) + ', dir=' + direction + ', open=' + str(open_p))
    print('    pnl_today=' + str(m.get('pnl_today')) + ', pnl_history=' + str(m.get('pnl_history')) + ', pnl_daily=' + str(m.get('pnl_daily')))

# 找SC L2节点
def find_nodes(nodes, key):
    results = []
    for n in nodes:
        if n.get('key') == key:
            results.append(n)
        if n.get('children'):
            results.extend(find_nodes(n['children'], key))
    return results

sc_l2 = find_nodes(d.get('tree', []), 'SC_2611')
print()
print('SC_2611 L2 node:')
if sc_l2:
    n = sc_l2[0]
    print('  key=' + n.get('key') + ', type=' + n.get('type'))
    print('  children count: ' + str(len(n.get('children', []))))
    print('  metrics: ' + str(n.get('metrics')))
    children = n.get('children', [])
    if children:
        first = children[0]
        print('  first child keys: ' + str(list(first.keys())))
        print('  first child sym=' + str(first.get('symbol')) + ' vol=' + str(first.get('volume')) + ' adj=' + str(first.get('adjust_price')) + ' last=' + str(first.get('last_price')))
