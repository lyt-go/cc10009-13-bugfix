"""DNS解析接口：维护解析配置、解析结果数据集与版本追溯。

接口分组：
- 配置清单 CRUD 与状态动作（提交测试/启用/降级）
- 解析刷新：POST /refresh，按域名加锁、幂等去重、旧版本拒绝覆盖
- 唯一解析结果数据集、版本快照、原始请求、失败来源查询
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dnsresolver import DnsresolverService

router = APIRouter(prefix="/api/dnsresolver", tags=["DNS解析"])

service = DnsresolverService()

LIST_FIELDS = ["配置编号", "域名模式", "DNS服务器", "解析超时毫秒", "缓存TTL秒", "A记录策略", "IPv6优先", "故障转移地址"]
STATUSES = ["草稿", "配置测试中", "已启用", "已降级"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按配置编号或域名模式检索"),
    status: str | None = Query(default=None, description="草稿、配置测试中、已启用、已降级"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按配置编号/域名模式与状态过滤DNS解析列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# 具体路径必须声明在 /{entry_id} 之前，否则会被路径参数吞掉。
@router.get("/resolutions", response_model=PageResult[dict])
def list_resolutions(
    domain: str | None = Query(default=None, description="按域名模式过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """读取唯一DNS解析结果数据集：每个域名模式只有一条。"""
    items = service.list_datasets()
    if domain:
        items = [row for row in items if row.get("域名模式") == domain]
    total = len(items)
    start = max(page - 1, 0) * size
    return PageResult(items=items[start:start + size], total=total, page=page, size=size)


@router.get("/resolutions/{domain}", response_model=dict)
def get_resolution(domain: str) -> dict[str, Any]:
    dataset = service.get_dataset(domain)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"域名模式 {domain} 暂无DNS解析结果数据集")
    return dataset


@router.get("/resolutions/{domain}/versions")
def list_resolution_versions(domain: str) -> dict[str, Any]:
    """返回该域名的版本快照链，冲突时两份版本都保留、优先级可核对。"""
    return {"域名模式": domain, "items": service.list_versions(domain)}


@router.get("/resolutions/{domain}/requests")
def list_resolution_requests(domain: str) -> dict[str, Any]:
    """原始解析请求与唯一解析结果数据集的对应关系。"""
    return {"域名模式": domain, "items": service.list_requests(domain)}


@router.get("/resolutions/{domain}/failures")
def list_resolution_failures(domain: str) -> dict[str, Any]:
    """失败来源与当时生效版本，页面与作业失败原因共用同一份证据。"""
    return {"域名模式": domain, "items": service.list_failures(domain)}


@router.post("/refresh", response_model=ActionResult)
def refresh_resolution(payload: EntryPayload) -> ActionResult:
    """触发一次DNS解析刷新：幂等去重、并发合并、旧版本拒绝覆盖缓存。"""
    dataset, message = service.refresh_resolution(payload.values)
    if dataset is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=dataset)


@router.get("/lookup", response_model=dict)
def lookup_resolution(host: str = Query(..., description="抓取作业实际访问的主机名")) -> dict[str, Any]:
    """采集执行侧读取某主机名当前唯一生效的解析版本。"""
    dataset = service.lookup(host)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"主机 {host} 没有生效中的DNS解析结果")
    return dataset


@router.post("/explain-failure", response_model=ActionResult)
def explain_failure(payload: EntryPayload) -> ActionResult:
    """按生效解析版本解释连接失败，返回同一份解析证据。"""
    host = str(payload.values.get("host") or payload.values.get("主机") or "").strip()
    reason = str(payload.values.get("reason") or payload.values.get("失败原因") or "连接失败").strip()
    if not host:
        return ActionResult(ok=False, message="缺少主机名，无法解释连接失败")
    explanation = service.explain_failure(host, reason)
    return ActionResult(ok=bool(explanation.get("可解释")), message=explanation["失败原因"], entry=explanation)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出DNS解析清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dnsresolver", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条解析配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"解析配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条解析配置，缺字段时说明原因而不是静默丢弃；全部解析参数随配置持久化。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="解析配置已登记", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """更新解析配置：域名模式、DNS参数、缓存TTL、IPv6优先、A记录策略、故障转移地址整体持久化。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条解析配置执行提交配置测试、启用配置、降级配置；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
