# VolTrading Dashboard

CTP 期货/期权实时监控看板。

## 架构

```
CTP (MD+TD)  ──►  Worker 线程（每秒定时）
                              │
                              ▼
              dashboard_v2/pricing.py      Black-76 Greeks + IV反推
              dashboard_v2/risk_engine.py  PnL三口径 + 树形聚合 + 标签
              dashboard_v2/settlement.py   结算单真实开仓成本加载
                              │
                              ▼  原子快照
              Flask API  ◄──  /api/dashboard  （只读，无任何计算）
                              │
                              ▼
              static/dashboard11.js  ──►  纯渲染，无业务逻辑
```

**核心原则**：单进程双线程、内存快照读写分离（零锁只读）、后端算好前端只画。

## 启动

```
C:\veighna_studio\pythonw.exe run_server.py
```

看板：http://127.0.0.1:5000/

局域网访问：http://192.168.50.194:5000/

Flask 进程：`C:/veighna_studio/pythonw.exe`

## 持仓查询机制

- 每次 API 调用 `get_all_positions()` 前，先发 `ReqQryInvestorPosition` 主动查询
- 等 100ms 让 TD 回调更新缓存，再读 `get_all_positions()`
- 查询路径：`api_server.py` → `engine.get_all_positions()`（Mock 模式或真实引擎）

## 文件说明

| 文件 | 作用 |
|------|------|
| `run_server.py` | 服务入口，Worker 线程 + Flask |
| `dashboard_v2/api_server.py` | Flask 工厂（`create_app()`）|
| `dashboard_v2/pricing.py` | Black-76 定价 + IV 二分法反推 |
| `dashboard_v2/risk_engine.py` | Greeks + PnL + 树形聚合 + 标签 |
| `dashboard_v2/settlement.py` | 结算单双策略成本加载 |
| `dashboard_v2/pricing.py` | 同上 |
| `static/dashboard11.js` | 前端渲染器 |
| `templates/dashboard.html` | 页面模板，引用 dashboard11.js |
| `ctp_config.json` | CTP 连接配置（用户名/密码/BrokerID 等）|
| `结算单/full_*.json` | 历史结算单（持仓成本基线）|
| `vnpy_api_server.py` | **旧版** API 服务（旧功能，未维护）|
| `vnpy_engine.py` | **旧版** 引擎封装（disconnect 修复版，未被 run_server.py 使用）|

## Greeks 计算

- 公式：Black-76
- IV 来源：优先用 `tick['iv']`，无则跳过期权 Greeks
- TTM：`max(contract['ttm'], 0.5/250)` 防止除零
- Theta：空头为正（时间价值衰减对空头有利）

## API 端点

| 路由 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 看板页面 |
| `/api/dashboard` | GET | 完整快照 JSON |
| `/api/ctp/status` | GET | CTP 心跳状态 |
| `/api/ctp/connect` | POST | 启动 CTP 引擎 |
| `/api/ctp/disconnect` | POST | 安全关闭连接 |

## 维护注意事项

1. **修改 dashboard_v2 模块**后直接重启服务即可生效
2. **修改 `run_server.py`** 后重启服务
3. **重启服务**：先杀进程再启动
   ```
   taskkill //F //PID <pid>
   C:\veighna_studio\pythonw.exe run_server.py
   ```
4. **浏览器缓存**：dashboard11.js 改名（如 dashboard12.js）+ 刷新
5. **结算单存档**：`save_settlement.py` 拉当天，`save_all_settlements.py` 批量

## 已知限制

- CTP 历史结算单只能拉约 35 个交易日（服务器限制）
- disconnect 后服务进程崩溃（`gateway.close()` 与 CTP .pyd 内部线程冲突，修复见 `api_ctp_disconnect`）
- 持仓不更新：TD 成交回报路径可能断，需主动查询（见上节）
