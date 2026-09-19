"""实证：全链 tick 样本里，单边报价 / 成交价陈旧度 / Mark 该取谁 的真实分布。"""
import sys, numpy as np, pandas as pd

f = sys.argv[1]
usecols = ["time","current","volume","a1_p","b1_p","a1_v","b1_v","contract_code","underlying","trading_day"]
df = pd.read_csv(f, usecols=usecols, dtype={"time": str})

def to_sec(t):
    t = t.strip()
    h, m, s = int(t[:2]), int(t[2:4]), float(t[4:])
    return h*3600 + m*60 + s

df["sec"] = df["time"].map(to_sec)
df["day"] = pd.to_datetime(df["trading_day"], format="%Y%m%d")
# 夜盘跨零点：20:00 之后的 tick 归下一日，sec 加 24h 保证单调
df["tnow"] = df["day"] + pd.to_timedelta(df["sec"], unit="s")
df.loc[df["sec"] >= 20*3600, "tnow"] += pd.Timedelta(days=1)

n = len(df)
two = (df.a1_p > 0) & (df.b1_p > 0)
ask_only = (df.a1_p > 0) & ~(df.b1_p > 0)
bid_only = (df.b1_p > 0) & ~(df.a1_p > 0)
none_ = ~(df.a1_p > 0) & ~(df.b1_p > 0)

print(f"file={f.split('/')[-1]}  rows={n:,}  contracts={df.contract_code.nunique()}  days={df.trading_day.nunique()}")
print(f"双边 {(two.mean()*100):.1f}%   仅卖一 {(ask_only.mean()*100):.1f}%   仅买一 {(bid_only.mean()*100):.1f}%   无盘口 {(none_.mean()*100):.1f}%")
print(f"current>0 占比 {(df.current>0).mean()*100:.1f}%")

# 单边时，那个单边价 vs current 的偏离（以最小变动价位为单位，从数据反推 tick size）
one_sided = df[ask_only | bid_only].copy()
one_sided["side"] = one_sided.a1_p.where(ask_only, one_sided.b1_p)
ok = one_sided[(one_sided.current > 0) & (one_sided.side > 0)]
ts = np.min(np.abs(np.diff(np.sort(ok.current.unique()))))
print(f"反推最小变动价位≈{ts}")
dev = (ok.side - ok.current).abs() / ok.current
for q in [50,75,90,95,99]:
    print(f"  单边价 vs last 偏离 P{q} = {np.percentile(dev,q)*100:.1f}%")
print(f"  单边价 < last*0.5 的占比（65/506 那种荒谬值）= {(ok.side < ok.current*0.5).mean()*100:.2f}%")
print(f"  单边价 > last*2   的占比 = {(ok.side > ok.current*2).mean()*100:.2f}%")

# 双边时 mid vs last
tb = df[two & (df.current>0)].copy()
tb["mid"] = (tb.a1_p + tb.b1_p)/2
d2 = (tb.mid - tb.current).abs()/tb.current
print(f"双边: mid vs last 偏离 P50={np.percentile(d2,50)*100:.2f}% P95={np.percentile(d2,95)*100:.2f}%  锁价(b<=last<=a)占比 {((tb.b1_p<=tb.current)&(tb.current<=tb.a1_p)).mean()*100:.1f}%")
sp = (tb.a1_p-tb.b1_p).abs()/tb.mid
print(f"      相对价差 P50={np.percentile(sp,50)*100:.2f}% P95={np.percentile(sp,95)*100:.2f}%")

# 最后成交间隔：current 连续不变的时长
g = df.sort_values(["contract_code","tnow"]).copy()
g["chg"] = g.current != g.groupby("contract_code").current.shift()
g["since"] = g.groupby([g.contract_code, g.chg.cumsum()]).tnow.transform(lambda s: s.iloc[-1])
g["gap"] = (g.tnow - g["since"]).dt.total_seconds()
print("最后成交间隔（秒）分布：" + "  ".join(
    f"P{q}={np.nanpercentile(g.gap,q):.0f}" for q in [50,75,90,95,99]))
print(f"  gap>1800s(30min) 占比 {(g.gap>1800).mean()*100:.1f}%   gap>3600 占比 {(g.gap>3600).mean()*100:.1f}%")

# 交叉验证「印证」：同一合约，上一笔成交价投影 vs 当前盘口
print("\n=== 印证实证：无成交间隔内，用盘口/上笔成交推当前，误差多大 ===")
sub = g[(g.gap>60) & two & (g.current>0)].copy()
sub["F"] = sub.groupby("trading_day").apply(lambda x: np.nan, include_groups=False) if False else np.nan
print(f"可测样本 {len(sub):,}")
