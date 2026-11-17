"""DNS 解析持久化、版本切换与并发幂等的端到端验证（直接调服务层，免 httpx）。"""
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, ".")

from app.services.dnsresolver import DnsresolverService, host_from_url
from app.services.crawljob import CrawljobService
from app.store import store

dns = DnsresolverService()
jobs = CrawljobService()
failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        failures.append(name)


# 1. 配置登记：全部解析参数持久化，不丢字段
entry, missing = dns.create_entry({
    "配置编号": "DNSR-T1",
    "域名模式": "api.target.example",
    "DNS服务器": "10.0.0.53",
    "解析超时毫秒": "2500",
    "缓存TTL秒": "120",
    "A记录策略": "轮询",
    "IPv6优先": "是",
    "故障转移地址": "10.0.0.99, 10.0.0.100",
})
check("登记配置成功", entry is not None and not missing)
check("缓存TTL持久化", entry["缓存TTL秒"] == "120")
check("IPv6优先持久化", entry["IPv6优先"] == "是")
check("A记录策略持久化", entry["A记录策略"] == "轮询")
check("故障转移地址持久化", entry["故障转移地址"] == "10.0.0.99, 10.0.0.100")
cfg_id = entry["id"]

# 2. 更新配置：版本号推进，参数整体更新，其他字段不丢
entry, msg = dns.update_entry(cfg_id, {"缓存TTL秒": "300", "DNS服务器": "10.0.0.54"})
check("更新配置成功", entry["缓存TTL秒"] == "300" and entry["DNS服务器"] == "10.0.0.54")
check("更新后版本+1", entry["配置版本"] == 2)
check("更新保留其他字段", entry["IPv6优先"] == "是" and entry["故障转移地址"])

# 3. 启用配置 + 刷新解析
entry, msg = dns.run_action(cfg_id, "启用配置")
check("启用状态", entry["status"] == "已启用")
check("启用快照保存", entry["启用快照"]["缓存TTL秒"] == "300")

out, msg = dns.refresh_resolution({
    "域名模式": "api.target.example", "配置编号": "DNSR-T1",
    "幂等键": "req-1", "来源": "抓取作业",
    "解析地址": "203.0.113.10", "故障转移地址": "10.0.0.99",
})
check("首次刷新成功", out is not None, msg)
ds = out["数据集"]
check("数据集生效版本", ds["生效版本"] == "DNSR-T1#v2", str(ds["生效版本"]))
check("数据集生效地址", ds["生效地址"] == ["203.0.113.10"])
check("TTL写入数据集", ds["缓存TTL秒"] == 300)
check("IPv6策略写入数据集", ds["IPv6优先"] == "是")
check("原始请求可追溯", bool(ds["原始请求"]))
check("数据集每域名唯一",
      len([d for d in dns.list_datasets() if d["域名模式"] == "api.target.example"]) == 1)

# 4. 幂等：重复提交同一幂等键，只生效一次
out2, msg = dns.refresh_resolution({
    "域名模式": "api.target.example", "配置编号": "DNSR-T1",
    "幂等键": "req-1", "解析地址": "203.0.113.99",
})
check("幂等重复返回成功", out2 is not None)
check("幂等不覆盖缓存", out2["数据集"]["生效地址"] == ["203.0.113.10"])
idem = [x for x in dns.list_requests("api.target.example") if x.get("幂等键") == "req-1"]
check("幂等请求只存一份", len(idem) == 1, f"实际 {len(idem)} 条")

# 5. 旧请求不能覆盖新缓存：升到 v3 后让 v2 晚到
entry, _ = dns.update_entry(cfg_id, {"DNS服务器": "10.0.0.55"})
check("更新后版本到v3", entry["配置版本"] == 3)
out, msg = dns.refresh_resolution({
    "域名模式": "api.target.example", "配置编号": "DNSR-T1",
    "配置版本": 3, "幂等键": "req-2", "解析地址": "203.0.113.20",
})
check("v3刷新成功", out is not None, msg)
check("v3地址生效", out["数据集"]["生效地址"] == ["203.0.113.20"])
stale, msg = dns.refresh_resolution({
    "域名模式": "api.target.example", "配置编号": "DNSR-T1",
    "配置版本": 2, "幂等键": "req-stale", "解析地址": "198.51.100.66",
})
check("旧版本请求被拒绝", stale is None and "旧版本" in msg, msg)
ds = dns.get_dataset("api.target.example")
check("缓存仍是新版本地址", ds["生效地址"] == ["203.0.113.20"])
fails = dns.list_failures("api.target.example")
check("旧版本拒绝留失败来源", any("旧版本" in f["失败原因"] for f in fails))
check("失败记录带生效版本", all(f.get("生效版本") for f in fails if "旧版本" in f["失败原因"]))

# 6. 并发刷新同一域名：同版本同地址只存一份，生效版本唯一
def worker(i):
    return dns.refresh_resolution({
        "域名模式": "api.target.example", "配置编号": "DNSR-T1",
        "配置版本": 3, "幂等键": f"concurrent-{i}", "解析地址": "203.0.113.20",
    })

with ThreadPoolExecutor(max_workers=8) as pool:
    outs = list(pool.map(worker, range(8)))
check("并发刷新全部受理", all(o is not None for o, _ in outs))
versions = dns.list_versions("api.target.example")
v3 = [v for v in versions if v["配置版本"] == 3]
check("同版本同地址并发只存一份", len(v3) == 1, f"v3版本记录 {len(v3)} 条")
merged = [x for x in dns.list_requests("api.target.example") if x.get("处理结果") == "并发合并"]
check("并发请求有合并记录", len(merged) == 8, f"合并 {len(merged)} 条")
active = [v for v in dns.list_versions("api.target.example") if v["状态"] == "生效"]
check("生效版本唯一", len(active) == 1 and active[0]["地址"] == ["203.0.113.20"])

# 7. 版本冲突：不同业务面在同版本上各持一份地址，按优先级裁决，两份版本都保留
o_low, msg = dns.refresh_resolution({
    "域名模式": "api.target.example", "配置编号": "DNSR-T1",
    "配置版本": 3, "幂等键": "conflict-low", "解析地址": "198.51.103.10",
    "优先级": 1, "来源": "重试队列",
})
check("冲突请求受理", o_low is not None, msg)
o_high, msg = dns.refresh_resolution({
    "域名模式": "api.target.example", "配置编号": "DNSR-T1",
    "配置版本": 3, "幂等键": "conflict-high", "解析地址": "198.51.103.20",
    "优先级": 9, "来源": "配置页面",
})
check("高优先级冲突生效",
      o_high is not None and o_high["数据集"]["生效地址"] == ["198.51.103.20"], msg)
versions = dns.list_versions("api.target.example")
kept = [v for v in versions if v["地址"] in (["198.51.103.10"], ["198.51.103.20"])]
check("冲突两份版本都保留", len(kept) == 2)
check("低优先级版本标冲突历史",
      any(v["状态"] in ("冲突历史", "冲突保留") and v["地址"] == ["198.51.103.10"] for v in versions))
check("数据集标记冲突状态", dns.get_dataset("api.target.example")["结果状态"] == "冲突")
check("冲突来源可追溯",
      {v["来源"] for v in kept} == {"重试队列", "配置页面"})

# 8. 同域名重复启用不能产生多个有效配置
entry2, _ = dns.create_entry({
    "配置编号": "DNSR-T2", "域名模式": "api.target.example",
    "DNS服务器": "10.0.0.56", "缓存TTL秒": "60",
})
blocked, msg = dns.run_action(entry2["id"], "启用配置")
check("同域名第二配置启用被拦", blocked is None, msg)
again, msg = dns.run_action(cfg_id, "启用配置")
check("重复启用幂等", again is not None and "未重复" in msg, msg)
active_cfgs = [r for r in store.rows("dnsresolver")
               if r["status"] == "已启用" and r["域名模式"] == "api.target.example"]
check("同域名有效配置唯一", len(active_cfgs) == 1)

# 9. 降级后保留原启用版本快照
down, msg = dns.run_action(cfg_id, "降级配置")
check("降级成功", down is not None and down["status"] == "已降级", msg)
check("降级保留启用快照", down["启用快照"] is not None)
check("数据集标记降级", dns.get_dataset("api.target.example")["结果状态"] == "已降级")
check("降级保留版本快照",
      any(v["状态"] == "降级保留" for v in dns.list_versions("api.target.example")))

# 10. 采集执行侧：读取生效版本并解释连接失败
job, _ = jobs.create_entry({
    "作业编号": "CRAW-T1", "目标站点": "api.target.example",
    "起始URL": "http://api.target.example/path?x=1",
})
check("URL主机名解析", host_from_url(job["起始URL"]) == "api.target.example")
job, msg = jobs.check_dns(job, "连接超时")
check("作业DNS核对成功", "已按生效版本" in msg, msg)
check("作业记录生效版本", job["DNS生效版本"] is not None)
check("作业记录生效地址", job["DNS生效地址"] == ["198.51.103.20"])
check("失败原因按生效版本解释",
      "198.51.103.20" in job["DNS失败原因"] and "连接超时" in job["DNS失败原因"],
      job["DNS失败原因"])
check("证据数据集可追溯", job["DNS证据数据集"] == "api.target.example")
check("lookup通配/精确匹配",
      dns.lookup("api.target.example") is not None and dns.lookup("unknown.x.io") is None)
explain = dns.explain_failure("api.target.example", "连接被重置")
check("explain与作业同源证据", explain["可解释"] and "198.51.103.20" in explain["失败原因"])

# 11. 另一业务面刷新后，旧作业证据不被回改；新作业读到新版本
re_enabled, _ = dns.run_action(cfg_id, "启用配置")
check("降级后可重新启用", re_enabled["status"] == "已启用")
check("重新启用后数据集脱离降级状态",
      dns.get_dataset("api.target.example")["结果状态"] == "待刷新")
updated, _ = dns.update_entry(cfg_id, {"缓存TTL秒": "600"})
new_ver = updated["配置版本"]
o, msg = dns.refresh_resolution({
    "域名模式": "api.target.example", "配置编号": "DNSR-T1",
    "配置版本": new_ver, "幂等键": "req-other-plane",
    "解析地址": "203.0.113.77", "来源": "配置页面",
})
check("另一业务面刷新成功", o is not None and o["数据集"]["生效地址"] == ["203.0.113.77"], msg)
check("旧作业核对证据不被回改", job["DNS生效地址"] == ["198.51.103.20"])
job2, _ = jobs.create_entry({
    "作业编号": "CRAW-T2", "目标站点": "api.target.example",
    "起始URL": "http://api.target.example/new",
})
jobs.check_dns(job2)
check("新作业读到新版本地址", job2["DNS生效地址"] == ["203.0.113.77"])

# 12. 无匹配配置 / 未启用刷新给可读失败
o, msg = dns.refresh_resolution({"域名模式": "no-config.example", "配置编号": "NOPE"})
check("无配置刷新被拒", o is None and msg)
dns.create_entry({"配置编号": "DNSR-T3", "域名模式": "draft.example", "DNS服务器": "8.8.8.8"})
o, msg = dns.refresh_resolution({"域名模式": "draft.example", "配置编号": "DNSR-T3"})
check("未启用配置刷新被拒", o is None and "未启用" in msg, msg)
check("失败来源可追溯",
      any(f["域名模式"] == "draft.example" for f in dns.list_failures()))

# 13. 通配域名模式匹配
dns.create_entry({
    "配置编号": "DNSR-T4", "域名模式": "*.wild.example",
    "DNS服务器": "9.9.9.9", "缓存TTL秒": "30",
})
wild = dns.get_entry(max(r["id"] for r in store.rows("dnsresolver")))
dns.run_action(wild["id"], "启用配置")
dns.refresh_resolution({"域名模式": "*.wild.example", "配置编号": "DNSR-T4", "幂等键": "w1"})
check("通配模式lookup命中",
      dns.lookup("sub.wild.example") is not None and dns.lookup("wild.example") is None)

# 14. 运营概览只统计 17 个业务模块，运行态追溯表不计入
overview = store.overview()
check("概览模块数不含内部追溯表",
      len(overview["modules"]) == 17 and "dns_resolution_result" not in
      [m["name"] for m in overview["modules"]])

print()
print(f"总计 {len(failures)} 项失败: {failures}" if failures else "全部场景通过")
sys.exit(1 if failures else 0)
