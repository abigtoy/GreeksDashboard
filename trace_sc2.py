import json, subprocess, sys

r = subprocess.run(['curl', '-s', 'http://localhost:5000/api/dashboard'], capture_output=True, text=True)
d = json.loads(r.stdout)

def find_node(nodes, key):
    for n in nodes:
        if n.get('key') == key:
            return n
        if n.get('children'):
            result = find_node(n['children'], key)
            if result:
                return result
    return None

sc2611 = find_node(d.get('tree', []), 'SC_2611')
if sc2611:
    children = sc2611.get('children', [])
    print('SC_2611 children count: ' + str(len(children)))
    total_today = 0
    total_hist = 0
    total_daily = 0
    for i, c in enumerate(children):
        sym = c.get('symbol', '')
        m = c.get('metrics', {})
        pt = m.get('pnl_today')
        ph = m.get('pnl_history')
        pd = m.get('pnl_daily')
        vol = c.get('volume', 0)
        adj = c.get('adjust_price')
        last = c.get('last_price')
        open_p = c.get('open_price')
        direction = c.get('direction', '')
        print(str(i) + ': ' + sym + ' ' + direction + ' vol=' + str(vol) + ' adj=' + str(adj) + ' last=' + str(last))
        print('   open=' + str(open_p) + ' pnl_today=' + str(pt) + ' pnl_history=' + str(ph) + ' pnl_daily=' + str(pd))
        if pt is not None and pt == pt:
            total_today += pt
        if ph is not None and ph == ph:
            total_hist += ph
        if pd is not None and pd == pd:
            total_daily += pd
    print()
    print('Sum of children: pnl_today=' + str(total_today) + ' pnl_history=' + str(total_hist) + ' pnl_daily=' + str(total_daily))
    print('SC_2611 metrics: pnl_today=' + str(sc2611.get('metrics',{}).get('pnl_today')) + ' pnl_history=' + str(sc2611.get('metrics',{}).get('pnl_history')) + ' pnl_daily=' + str(sc2611.get('metrics',{}).get('pnl_daily')))
