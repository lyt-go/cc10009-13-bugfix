"""DNS解析业务规则：配置持久化、版本切换与唯一解析结果数据集。

设计要点（对应故障口径）：

1. 配置持久化：域名模式、DNS参数、缓存TTL、IPv6优先、A记录策略、故障转移地址
   全部随配置落库，任何更新都保留在同一条配置上，不丢字段。
2. 唯一有效版本：同一域名模式只允许一条「已启用」配置；重复启用幂等处理，
   不会产生多个有效配置；降级后原启用版本快照仍保留可追溯。
3. 唯一解析结果数据集：每个域名模式在 ``dns_resolution_result`` 表中只有一条
   数据集，原始请求（``dns_resolution_request``）、版本快照
   （``dns_resolution_version``）、失败来源（``dns_resolution_failure``）
   都通过域名模式与数据集建立对应关系。
4. 并发与幂等：刷新按域名模式加锁，同域名并发刷新串行进入临界区；幂等键重复
   提交直接回放首次结果；同版本同地址的并发结果只保存一份。
5. 旧版本保护：请求携带的配置版本低于当前生效版本时拒绝写缓存，旧请求不能把
   新版本地址覆盖回去；版本冲突时两份版本都保留，按优先级决定谁生效。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from app.store import store

MODULE = "dnsresolver"
# 唯一解析结果数据集及三类追溯表
RESULT_MODULE = "dns_resolution_result"
REQUEST_MODULE = "dns_resolution_request"
VERSION_MODULE = "dns_resolution_version"
FAILURE_MODULE = "dns_resolution_failure"

REQUIRED_FIELDS = ["配置编号", "域名模式", "DNS服务器"]
# 必须随配置一起持久化的全部解析参数字段
ALL_FIELDS = [
    "配置编号",
    "域名模式",
    "DNS服务器",
    "解析超时毫秒",
    "缓存TTL秒",
    "A记录策略",
    "IPv6优先",
    "故障转移地址",
]
STATUS_ORDER = ["草稿", "配置测试中", "已启用", "已降级"]
ACTION_RULES = {"提交配置测试": "配置测试中", "启用配置": "已启用", "降级配置": "已降级"}
NEGATIVE_ACTIONS = []

DEFAULT_TTL_SECONDS = 300
DEFAULT_TIMEOUT_MS = 3000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _split_addresses(value: Any) -> list[str]:
    """故障转移地址等字段允许用逗号/分号/空白分隔，统一拆成列表。"""
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).replace("；", ",").replace(";", ",").replace("，", ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def _mock_addresses(domain: str, version: int, ipv6_first: bool) -> list[str]:
    """没有外部 DNS 网络时的确定性解析结果：同域名同版本永远得到同一组地址。"""
    digest = hashlib.md5(f"{domain}#v{version}".encode("utf-8")).hexdigest()
    octets = [int(digest[i:i + 2], 16) for i in range(0, 8, 2)]
    ipv4 = "10.{}.{}.{}".format(octets[1], octets[2], octets[3] or 1)
    ipv6 = "fd00::{:x}:{:x}:{:x}".format(octets[1], octets[2], octets[3] or 1)
    return [ipv6, ipv4] if ipv6_first else [ipv4, ipv6]


def _domain_matches(pattern: str, host: str) -> bool:
    """域名模式支持精确匹配与 ``*.example.com`` 形式的后缀匹配。"""
    pattern = (pattern or "").strip().lower()
    host = (host or "").strip().lower()
    if not pattern or not host:
        return False
    if pattern == host:
        return True
    if pattern.startswith("*."):
        suffix = pattern[1:]  # ".example.com"
        return host.endswith(suffix)
    return False


class DnsresolverService:
    # ------------------------------------------------------------------ 清单
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
            rows = [
                row
                for row in rows
                if keyword in str(row.get("配置编号", ""))
                or keyword in str(row.get("域名模式", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    # ------------------------------------------------------------ 配置持久化
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        entry: dict[str, Any] = {"id": store.next_id(MODULE)}
        # 全部解析参数都持久化，而不是只留必填的三个字段。
        for field in ALL_FIELDS:
            entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["配置版本"] = 1
        entry["启用版本"] = None
        entry["启用快照"] = None
        entry["更新时间"] = _now()
        store.rows(MODULE).append(entry)
        return entry, []

    def update_entry(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"解析配置 {entry_id} 不存在或已归档"
        changed = False
        for field in ALL_FIELDS:
            if field in values and values.get(field) != entry.get(field):
                entry[field] = values.get(field)
                changed = True
        if changed:
            entry["配置版本"] = _as_int(entry.get("配置版本"), 1) + 1
            entry["更新时间"] = _now()
            # 已启用配置被修改：快照与数据集版本同步推进，缓存标记待刷新，
            # 这样随后到达的旧版本请求会被版本保护拦下。
            if entry.get("status") == "已启用":
                entry["启用版本"] = entry["配置版本"]
                entry["启用快照"] = self._snapshot(entry)
                dataset = self._ensure_dataset(str(entry["域名模式"]), entry)
                dataset["生效配置版本"] = entry["配置版本"]
                dataset["生效版本"] = self._version_key(entry)
                dataset["结果状态"] = "待刷新"
                dataset["更新时间"] = _now()
        return entry, "解析配置已更新" if changed else "解析配置无字段变化"

    # ------------------------------------------------------------ 状态流转
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"解析配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于DNS解析可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        if action == "启用配置":
            return self._enable(entry)
        if action == "降级配置":
            return self._downgrade(entry)

        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        entry["更新时间"] = _now()
        return entry, f"解析配置已{action}"

    def _enable(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str]:
        domain = str(entry.get("域名模式") or "").strip()
        # 同一域名模式只能有一个生效配置：重复启用同一条幂等成功，
        # 启用别的配置则直接拦下，避免出现多个有效版本。
        if entry.get("status") == "已启用":
            return entry, "解析配置已处于启用状态，未重复生成有效配置"
        for other in store.rows(MODULE):
            if (
                other is not entry
                and other.get("status") == "已启用"
                and str(other.get("域名模式") or "").strip() == domain
            ):
                return (
                    None,
                    f"域名模式 {domain} 已存在生效配置 {other.get('配置编号')}，"
                    "请先降级原配置再启用",
                )
        entry["status"] = "已启用"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["配置版本"] = _as_int(entry.get("配置版本"), 1)
        entry["启用版本"] = entry["配置版本"]
        entry["启用快照"] = self._snapshot(entry)
        entry["更新时间"] = _now()
        dataset = self._ensure_dataset(domain, entry)
        # 降级后重新启用：数据集不能停留在“已降级”，标记待刷新等下一次解析。
        if dataset.get("结果状态") == "已降级":
            dataset["结果状态"] = "待刷新"
            dataset["更新时间"] = _now()
        else:
            dataset["结果状态"] = dataset.get("结果状态") or "待刷新"
        return entry, "解析配置已启用，启用版本快照已保存"

    def _downgrade(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str]:
        domain = str(entry.get("域名模式") or "").strip()
        entry["status"] = "已降级"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["降级时间"] = _now()
        # 启用版本快照原样保留，供页面与作业失败原因回溯同一份证据。
        dataset = self._find_dataset(domain)
        if dataset is not None:
            dataset["结果状态"] = "已降级"
            dataset["更新时间"] = _now()
            store.rows(VERSION_MODULE).append({
                "id": store.next_id(VERSION_MODULE),
                "域名模式": domain,
                "配置编号": entry.get("配置编号"),
                "配置版本": entry.get("启用版本"),
                "版本键": self._version_key(entry, entry.get("启用版本")),
                "状态": "降级保留",
                "优先级": 0,
                "来源": "配置页面",
                "创建时间": _now(),
                "地址": list(dataset.get("生效地址", [])),
            })
        return entry, "解析配置已降级，原启用版本快照已保留"

    # ------------------------------------------------------------ 解析刷新
    def refresh_resolution(
        self, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        domain = str(values.get("域名模式") or "").strip()
        config_code = values.get("配置编号")
        config = self._find_config(domain, config_code)
        if config is None and config_code is not None:
            entry = next(
                (row for row in store.rows(MODULE) if row.get("配置编号") == config_code),
                None,
            )
            if entry is not None:
                config = entry
                domain = str(entry.get("域名模式") or "").strip()
        if not domain:
            return None, "缺少域名模式，无法刷新DNS解析结果"
        if config is None:
            return None, f"域名模式 {domain} 没有匹配的解析配置"

        # 同一域名模式的刷新串行化：并发刷新只可能留下一个生效版本。
        with store.lock_for(domain):
            idem_key = values.get("幂等键")
            if idem_key:
                repeat = next(
                    (
                        row
                        for row in store.rows(REQUEST_MODULE)
                        if row.get("幂等键") == idem_key
                        and row.get("域名模式") == domain
                    ),
                    None,
                )
                if repeat is not None:
                    # 重复提交按幂等回放首次结论，不再保存第二份结果。
                    dataset = self._find_dataset(domain)
                    return self._refresh_outcome(dataset, repeat), (
                        f"幂等键 {idem_key} 已提交过，按首次结果生效一次"
                    )

            if config.get("status") != "已启用":
                return self._record_failure(
                    config,
                    values,
                    f"解析配置状态为{config.get('status')}，未启用不能刷新缓存",
                )

            payload_version = _as_int(values.get("配置版本"), _as_int(config.get("配置版本"), 1))
            priority = _as_int(values.get("优先级"), 0)
            source = str(values.get("来源") or "抓取作业")
            addresses = _split_addresses(values.get("解析地址"))
            failover = _split_addresses(values.get("故障转移地址")) or _split_addresses(
                config.get("故障转移地址")
            )
            if not addresses:
                ipv6_first = str(config.get("IPv6优先") or "").strip().lower() in {"是", "true", "1", "yes"}
                addresses = _mock_addresses(domain, payload_version, ipv6_first)
            ttl = _as_int(config.get("缓存TTL秒"), DEFAULT_TTL_SECONDS) or DEFAULT_TTL_SECONDS

            dataset = self._ensure_dataset(domain, config)
            effective_version = _as_int(dataset.get("生效配置版本"), 0)
            request_row: dict[str, Any] = {
                "id": store.next_id(REQUEST_MODULE),
                "域名模式": domain,
                "配置编号": config.get("配置编号"),
                "配置版本": payload_version,
                "幂等键": idem_key,
                "来源": source,
                "解析地址输入": list(addresses),
                "故障转移输入": list(failover),
                "请求时间": _now(),
                "结果数据集": domain,
                "处理结果": "",
            }

            failure_reason = values.get("失败原因")
            if failure_reason:
                return self._record_failure(config, values, str(failure_reason), request_row)

            # 旧请求保护：携带版本比生效版本旧时，绝不允许覆盖新缓存。
            if payload_version < effective_version:
                request_row["处理结果"] = "旧版本拒绝"
                store.rows(REQUEST_MODULE).append(request_row)
                store.rows(FAILURE_MODULE).append({
                    "id": store.next_id(FAILURE_MODULE),
                    "域名模式": domain,
                    "配置编号": config.get("配置编号"),
                    "配置版本": payload_version,
                    "幂等键": idem_key,
                    "来源": source,
                    "失败原因": (
                        f"旧版本请求(v{payload_version})晚到，当前生效版本为"
                        f"v{effective_version}，已拒绝写缓存"
                    ),
                    "失败时间": _now(),
                    "生效版本": dataset.get("生效版本"),
                    "结果数据集": domain,
                })
                dataset["最近失败"] = {
                    "原因": "旧版本请求被拒绝",
                    "请求版本": payload_version,
                    "生效版本": effective_version,
                    "时间": _now(),
                }
                return None, (
                    f"v{payload_version} 是旧版本解析请求，当前生效版本为 "
                    f"v{effective_version}，不能覆盖缓存"
                )

            same_version = self._version_row(domain, config.get("配置编号"), payload_version)
            if (
                same_version is not None
                and same_version.get("状态") == "生效"
                and list(same_version.get("地址", [])) == addresses
            ):
                # 同版本同地址的并发/重复刷新：只保存一份结果。
                request_row["处理结果"] = "并发合并"
                request_row["版本id"] = same_version.get("id")
                store.rows(REQUEST_MODULE).append(request_row)
                return self._refresh_outcome(dataset, request_row), "并发刷新已合并，只保留一份解析结果"

            # 同版本不同地址：
            # - 同一来源（如抓取作业自己重刷）视为该版本地址的正常更新，直接替换缓存；
            # - 不同来源在同版本上各持一份地址才是版本冲突，两份版本都保留并按优先级裁决。
            current = self._effective_version_row(dataset)
            is_conflict = (
                payload_version == effective_version
                and effective_version > 0
                and current is not None
                and current.get("来源") != source
            )

            version_row = {
                "id": store.next_id(VERSION_MODULE),
                "域名模式": domain,
                "配置编号": config.get("配置编号"),
                "配置版本": payload_version,
                "版本键": self._version_key(config, payload_version),
                "地址": list(addresses),
                "故障转移地址": list(failover),
                "优先级": priority,
                "来源": source,
                "幂等键": idem_key,
                "状态": "生效",
                "创建时间": _now(),
                "失效时间": None,
            }

            if is_conflict:
                if priority > _as_int(current.get("优先级"), 0):
                    current["状态"] = "冲突历史"
                    current["失效时间"] = _now()
                    dataset.update(self._cache_fields(config, addresses, failover, ttl, version_row, source))
                    dataset["结果状态"] = "冲突"
                    request_row["处理结果"] = "冲突并入"
                    message = "版本冲突，高优先级版本已生效，旧版本保留可追溯"
                else:
                    version_row["状态"] = "冲突保留"
                    request_row["处理结果"] = "冲突并入"
                    message = "版本冲突，新生效版本优先级不占优，两份版本均已保留"
            else:
                # 新版本或同来源同版本更新：旧生效版本降为历史但不删除，
                # 新版本成为唯一生效版本，缓存立即替换。
                if current is not None:
                    current["状态"] = "历史"
                    current["失效时间"] = _now()
                request_row["处理结果"] = "已生效"
                dataset.update(self._cache_fields(config, addresses, failover, ttl, version_row, source))
                dataset["结果状态"] = "生效"
                message = f"解析结果已刷新，生效版本 {version_row['版本键']}"

            store.rows(VERSION_MODULE).append(version_row)
            request_row["版本id"] = version_row["id"]
            store.rows(REQUEST_MODULE).append(request_row)
            dataset["更新时间"] = _now()
            dataset["版本历史"] = [row["id"] for row in store.rows(VERSION_MODULE) if row.get("域名模式") == domain]
            dataset["原始请求"] = [row["id"] for row in store.rows(REQUEST_MODULE) if row.get("域名模式") == domain]
            return self._refresh_outcome(dataset, request_row), message

    # ------------------------------------------------------------ 数据集查询
    def list_datasets(self) -> list[dict[str, Any]]:
        return store.rows(RESULT_MODULE)

    def get_dataset(self, domain: str) -> dict[str, Any] | None:
        return self._find_dataset(domain)

    def list_requests(self, domain: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(REQUEST_MODULE)
        if domain:
            rows = [row for row in rows if row.get("域名模式") == domain]
        return rows

    def list_failures(self, domain: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(FAILURE_MODULE)
        if domain:
            rows = [row for row in rows if row.get("域名模式") == domain]
        return rows

    def list_versions(self, domain: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(VERSION_MODULE)
        if domain:
            rows = [row for row in rows if row.get("域名模式") == domain]
        return rows

    def lookup(self, host: str) -> dict[str, Any] | None:
        """采集执行侧按实际主机名读取唯一生效解析数据集。"""
        host = (host or "").strip().lower()
        if not host:
            return None
        for dataset in store.rows(RESULT_MODULE):
            if _domain_matches(str(dataset.get("域名模式") or ""), host):
                return dataset
        return None

    def explain_failure(self, host: str, reason: str) -> dict[str, Any]:
        """按生效解析版本解释一次连接失败，页面与作业共用同一份解析证据。"""
        dataset = self.lookup(host)
        if dataset is None:
            return {
                "主机": host,
                "可解释": False,
                "失败原因": f"{reason}：域名 {host} 没有生效中的DNS解析结果",
                "生效版本": None,
            }
        return {
            "主机": host,
            "可解释": True,
            "失败原因": f"{reason}：按生效版本 {dataset.get('生效版本')} 的地址 "
            f"{','.join(dataset.get('生效地址', []))} 连接仍失败",
            "生效版本": dataset.get("生效版本"),
            "生效地址": list(dataset.get("生效地址", [])),
            "结果数据集": dataset.get("域名模式"),
        }

    # ------------------------------------------------------------ 内部工具
    def _find_config(self, domain: str, config_code: Any) -> dict[str, Any] | None:
        if config_code is not None:
            hit = next(
                (row for row in store.rows(MODULE) if row.get("配置编号") == config_code),
                None,
            )
            if hit is not None:
                return hit
        candidates = [
            row
            for row in store.rows(MODULE)
            if str(row.get("域名模式") or "").strip() == domain
        ]
        enabled = next((row for row in candidates if row.get("status") == "已启用"), None)
        return enabled or (candidates[0] if candidates else None)

    def _find_dataset(self, domain: str) -> dict[str, Any] | None:
        return next(
            (row for row in store.rows(RESULT_MODULE) if row.get("域名模式") == domain),
            None,
        )

    def _ensure_dataset(self, domain: str, config: dict[str, Any]) -> dict[str, Any]:
        dataset = self._find_dataset(domain)
        if dataset is None:
            dataset = {
                "id": domain,
                "域名模式": domain,
                "生效配置编号": config.get("配置编号"),
                "生效版本": None,
                "生效配置版本": 0,
                "生效地址": [],
                "故障转移地址": [],
                "缓存TTL秒": _as_int(config.get("缓存TTL秒"), DEFAULT_TTL_SECONDS) or DEFAULT_TTL_SECONDS,
                "IPv6优先": config.get("IPv6优先"),
                "A记录策略": config.get("A记录策略"),
                "DNS服务器": config.get("DNS服务器"),
                "解析超时毫秒": _as_int(config.get("解析超时毫秒"), DEFAULT_TIMEOUT_MS) or DEFAULT_TIMEOUT_MS,
                "刷新来源": None,
                "刷新时间": None,
                "结果状态": "待刷新",
                "版本历史": [],
                "原始请求": [],
                "最近失败": None,
                "创建时间": _now(),
                "更新时间": _now(),
            }
            store.rows(RESULT_MODULE).append(dataset)
        return dataset

    def _cache_fields(
        self,
        config: dict[str, Any],
        addresses: list[str],
        failover: list[str],
        ttl: int,
        version_row: dict[str, Any],
        source: str,
    ) -> dict[str, Any]:
        return {
            "生效配置编号": config.get("配置编号"),
            "生效版本": version_row["版本键"],
            "生效配置版本": version_row["配置版本"],
            "生效地址": list(addresses),
            "故障转移地址": list(failover),
            "缓存TTL秒": ttl,
            "IPv6优先": config.get("IPv6优先"),
            "A记录策略": config.get("A记录策略"),
            "DNS服务器": config.get("DNS服务器"),
            "解析超时毫秒": _as_int(config.get("解析超时毫秒"), DEFAULT_TIMEOUT_MS) or DEFAULT_TIMEOUT_MS,
            "刷新来源": source,
            "刷新时间": _now(),
            "最近失败": None,
        }

    def _version_row(
        self, domain: str, config_code: Any, version: int
    ) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in store.rows(VERSION_MODULE)
                if row.get("域名模式") == domain
                and row.get("配置编号") == config_code
                and _as_int(row.get("配置版本")) == version
            ),
            None,
        )

    def _effective_version_row(self, dataset: dict[str, Any]) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in store.rows(VERSION_MODULE)
                if row.get("域名模式") == dataset.get("域名模式")
                and row.get("状态") == "生效"
            ),
            None,
        )

    def _record_failure(
        self,
        config: dict[str, Any],
        values: dict[str, Any],
        reason: str,
        request_row: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        domain = str(config.get("域名模式") or "").strip()
        with store.lock_for(domain):
            dataset = self._ensure_dataset(domain, config)
            failure = {
                "id": store.next_id(FAILURE_MODULE),
                "域名模式": domain,
                "配置编号": config.get("配置编号"),
                "配置版本": _as_int(values.get("配置版本"), _as_int(config.get("配置版本"), 1)),
                "幂等键": values.get("幂等键"),
                "来源": values.get("来源") or "抓取作业",
                "失败原因": reason,
                "失败时间": _now(),
                "生效版本": dataset.get("生效版本"),
                "结果数据集": domain,
            }
            store.rows(FAILURE_MODULE).append(failure)
            if request_row is None:
                request_row = {
                    "id": store.next_id(REQUEST_MODULE),
                    "域名模式": domain,
                    "配置编号": config.get("配置编号"),
                    "配置版本": failure["配置版本"],
                    "幂等键": values.get("幂等键"),
                    "来源": failure["来源"],
                    "解析地址输入": _split_addresses(values.get("解析地址")),
                    "故障转移输入": _split_addresses(values.get("故障转移地址")),
                    "请求时间": _now(),
                    "结果数据集": domain,
                    "处理结果": "",
                }
            request_row["处理结果"] = "失败记录"
            store.rows(REQUEST_MODULE).append(request_row)
            dataset["最近失败"] = {
                "原因": reason,
                "失败来源": failure["来源"],
                "生效版本": dataset.get("生效版本"),
                "时间": failure["失败时间"],
                "失败记录id": failure["id"],
            }
            dataset["原始请求"] = [
                row["id"] for row in store.rows(REQUEST_MODULE) if row.get("域名模式") == domain
            ]
            dataset["更新时间"] = _now()
            return None, reason

    def _refresh_outcome(
        self, dataset: dict[str, Any] | None, request_row: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "数据集": dataset,
            "处理结果": request_row.get("处理结果"),
            "请求id": request_row.get("id"),
            "结果数据集": request_row.get("结果数据集"),
        }

    def _snapshot(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {field: entry.get(field) for field in ALL_FIELDS} | {
            "配置版本": entry.get("配置版本"),
            "快照时间": _now(),
        }

    def _version_key(self, entry: dict[str, Any], version: Any = None) -> str:
        ver = version if version is not None else entry.get("配置版本")
        return f"{entry.get('配置编号')}#v{ver}"


def host_from_url(url: str) -> str:
    """从抓取作业的起始URL或站点字段里取出主机名，供采集执行侧核对解析结果。"""
    text = (url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"http://{text}"
    return urlparse(text).hostname or ""
