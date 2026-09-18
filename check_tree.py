import json, sys, subprocess

r = subprocess.run(['curl', '-s', 'http://localhost:5000/api/dashboard'], capture_output=True, text=True)
d = json.loads(r.stdout)

def walk(nodes, depth=0):
    for n in nodes:
        prefix = '  ' * depth
        tp = n.get('type','')
        key = n.get('key','')
        sym = n.get('symbol','')
        und = n.get('underlying','')
        pnl = n.get('metrics',{}).get('pnl_today') if n.get('metrics') else None
        label = key or sym or und
        print(prefix + '[' + tp + '] ' + label + ' pnl_today=' + str(pnl))
        if n.get('children'):
            walk(n['children'], depth+1)

walk(d.get('tree', []))