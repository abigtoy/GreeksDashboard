
import sys, os, time
sys.path.insert(0, 'C:/Quant_2026/期货执行策略/GreeksDashboard_v0.1')
from dashboard_v2.api_server import _engine
if _engine is None:
    print("ENGINE_IS_NONE")
    sys.exit(1)
try:
    gw = _engine.main_engine.gateways.get('CTP') if (_engine and _engine.main_engine) else None
    if gw and hasattr(gw, 'td_api'):
        td = gw.td_api
        print(f"CTP_TD_LOGGED_IN={td.login_status if hasattr(td, 'login_status') else 'N/A'}")
        print(f"TRADING_DAY={td.trading_day if hasattr(td, 'trading_day') else 'N/A'}")
        # 尝试查询交易（如果 API 支持）
        # 注意：直接调用 QryTrade 可能需要异步处理，这里只尝试访问属性
        print(f"GATEWAY_EXISTS={gw is not None}")
    else:
        print("GATEWAY_NOT_FOUND")
except Exception as e:
    print(f"ENGINE_QUERY_ERROR={e}")
