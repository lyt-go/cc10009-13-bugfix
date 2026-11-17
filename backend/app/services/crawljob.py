"""抓取作业业务规则：状态流转、字段校验、DNS生效版本核对。

采集执行侧不自行缓存DNS地址，而是读取 DNS 解析模块里唯一解析结果数据集的
生效版本，并按该版本解释连接失败；核对结果直接反映到作业清单字段上，供页面
核对。另一业务面随后刷新DNS解析结果时，旧作业持有的版本证据仍可追溯。
"""
from __future__ import annotations

from typing import Any

from app.services.dnsresolver import DnsresolverService, host_from_url
from app.store import store

MODULE = "crawljob"
REQUIRED_FIELDS = ["作业编号", "目标站点", "起始URL"]
STATUS_ORDER = ["草稿", "作业审批中", "执行中", "已完成", "已停止"]
ACTION_RULES = {"提交作业审批": "作业审批中", "启动作业": "执行中", "停止作业": "已停止", "DNS核对": "执行中"}
NEGATIVE_ACTIONS = []

dns_service = DnsresolverService()


class CrawljobService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("作业编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"作业定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于抓取作业可执行范围"
        if action == "DNS核对":
            return self.check_dns(entry)
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"作业定义已{action}"

    def check_dns(self, entry: dict[str, Any], failure_reason: str = "连接失败") -> tuple[dict[str, Any], str]:
        """按DNS解析结果中的生效版本核对作业地址，并解释连接失败。"""
        host = host_from_url(str(entry.get("起始URL") or "")) or str(entry.get("目标站点") or "").strip()
        dataset = dns_service.lookup(host)
        if dataset is None:
            entry["DNS核对状态"] = "无生效解析"
            entry["DNS生效版本"] = None
            entry["DNS生效地址"] = []
            entry["DNS失败原因"] = f"主机 {host} 没有生效中的DNS解析结果"
            entry["DNS证据数据集"] = None
            return entry, f"作业 {entry.get('作业编号')} 未找到主机 {host} 的生效DNS解析版本"

        # 作业侧只记录生效版本证据；后续DNS刷新产生新版本也不会回改这份核对结果。
        entry["DNS核对状态"] = "已核对"
        entry["DNS生效版本"] = dataset.get("生效版本")
        entry["DNS生效地址"] = list(dataset.get("生效地址", []))
        entry["DNS故障转移地址"] = list(dataset.get("故障转移地址", []))
        entry["DNS证据数据集"] = dataset.get("域名模式")
        entry["DNS缓存TTL秒"] = dataset.get("缓存TTL秒")
        explanation = dns_service.explain_failure(host, failure_reason)
        entry["DNS失败原因"] = explanation["失败原因"]
        return entry, (
            f"作业 {entry.get('作业编号')} 已按生效版本 {dataset.get('生效版本')} 完成DNS核对，"
            f"连接失败按该版本地址解释"
        )
