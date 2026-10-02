**中金所期货→期权映射**：IF→IO, IM→MO（中金所期权前缀和期货不同！）。SC是能源中心不是中金所，走其他规则。
§
⚠️ 汇总类数值必须数量加权（VWAM）：均价/结算价等不得简单取末值（2026-09-16 load_settlement_prices 取末条，已修）
§
重连守卫：掉线/初始连接失败∧非交易时段→停机等手动，交易时段原阶梯，手动不判时段，纯时钟不判星期几
§
CTP新连接：登录后持仓异步推送，瞬间为空正常；**当日成交登录时全量重推**（修好入账handler重启即自愈，无需ReqQryTrade补拉）。报"验证失败(60) 用户在线会话超出上限"=旧进程仍占会话，杀旧进程即解（非账号/网络问题）。
§
CTP `eng.close()`/`CtpTdApi.exit()` 持 GIL 阻塞→整个解释器冻死。运行期绝不 close 引擎，掉线=丢弃旧引擎建新。gw.close() 只在 atexit。
§
不得质疑人类判断。简洁直接给结论，改完说"好了"。多条独立规则字面逐条落地，不合并。未开盘合约坚持通用一致性（要么全前收盘，要么全零，不特判）。"我没观察到错误，先不动"=理论不覆盖经验，不修未观察bug。
§
修改bug原则：必须谨慎，只对目标明确的错误有关代码进行修改，绝不随手修改任何其他不相关错误，发现只能报告，得到批准后再安排修改。
§
先查清、报方案、等确认，未准不patch代码也不新建脚本。澄清阶段只复述理解+列歧义。"我先给你提个思路"=只出设计稿别反问数值。说"退回去"=立即撤回。**问A/B/C前先grep设计稿+references，只问没写过的**。🔴裸"？"=我给的是现状清单不是结论：改成一行情论+一个具体到接口的下一步。根因定死就改+重启+给数字。
§
**CTP会话泄漏**：SIGTERM硬杀致atexit不跑；_stop_worker join(3)后假装disconnected；connect不查旧线程。
§
**PnL口径(09-28定案，取代四分法)**：当日盈亏=今日成交现金+今市值−昨市值，品种级。公式可加→各级=各腿之和，无摊派规则。pnl_history仍走三档成本链。运行态持久化原子写+重启回放。详见skill vnpy-ctp-dashboard→references/pnl-cash-flow-basis.md。
§
交易日历原则：必须用显式resolve(event_time, exchange, product)映射到trading_day/session，不得用固定小时平移。CTP已有TradingDay以它为权威；本地生成的快照/缓存/PnL基准按trading_day分区。时间平移只能是派生视图，不能用于账务/快照/缓存清理。
§
**VeighNa 环境**：`C:\veighna_studio\python.exe`。重启后须 POST /api/ctp/connect 才连CTP（worker不自启；**空body即可**→回退ctp_accounts.json；16s内持仓0=正常别重复connect）→ skill vnpy-ctp-dashboard 重连。事件handler入参是Event，数据在event.data（未拆包→成交全被去重静默丢）。代码每天17点自动提交，别手动commit。⚠️veighna_studio **无 pyarrow**，只能跑实盘；量化研究用 `C:\Users\H\anaconda3\python.exe`。
§
CTP option_type 是中文（看涨期权/看跌期权，期货为空）；判 C/P 须用 cp_from_symbol。树 key：L2=IM_2612，L3=MO2612-P-6400。
§
结算单 sync 每日20:20跑（扫近30天缺漏旧→新补下）→ 日内结算单必然落后一交易日，不是缺数据，别当异常查。
§
PnL调试纪律：昨仓对→查开仓价取用链；禁从记忆发明价格差异。🔴**归因纪律**：「能复现这个数」≠「本案走这条」——报根因前先问线上真实输入能否进入该路径。锁同一时间截面。用户以外部权威否掉前提时整链撤回，别抢救已给出的修法。
§
期权市值口径(09-24/09-29)：市值权益=balance+期权净市值(多头正/义务仓负，last×|vol|×size逐腿，L3→L2→L1)。**无行情时last用T-1 close_avg兜底**(MO/IF/IC/IO无夜盘，没开盘≠没价值，否掉"无价不计入")。balance≠权益。
§
未开盘合约一致性（09-29）：无夜盘品种(IF/IC/MO)保持T-1收盘；须全有或全归零，禁标的0+期权有价致Black-76下溢假0。统读T-1快照leaves与raw.underlying。见skill。
§
⚠️CTP tick.datetime带tzinfo(Asia/Shanghai)，与naive now()相减抛TypeError被except吞→报价静默清零→IV全组同值。_is_stale须先 astimezone().replace(tzinfo=None)，且留本地墙钟不换服务器时间。