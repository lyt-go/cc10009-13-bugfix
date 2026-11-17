"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

DNS 解析的运行态数据（唯一解析结果数据集、原始解析请求、版本快照、失败来源）
也放在这里：相关表按需创建，配合按域名模式加锁，保证同一域名的并发刷新只有
一个临界区，最终只留下一份生效解析版本。
"""
from __future__ import annotations

import threading
from typing import Any

from app.seed import SEED_ROWS

# DNS 解析运行态表：只做内部追溯，不计入运营概览的业务模块数。
INTERNAL_TABLES = {
    "dns_resolution_result",
    "dns_resolution_request",
    "dns_resolution_version",
    "dns_resolution_failure",
}


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 每个域名模式一把锁：并发刷新同一域名时串行进入临界区。
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in INTERNAL_TABLES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        wanted = str(entry_id)
        for row in self.rows(module):
            raw_id = row.get("id", 0)
            # 业务表 id 是自增整数，DNS 结果数据集 id 是域名模式字符串，两种都支持。
            if str(raw_id) == wanted:
                return row
            try:
                if int(raw_id) == int(entry_id):
                    return row
            except (TypeError, ValueError):
                continue
        return None

    def lock_for(self, key: str) -> threading.RLock:
        """取域名模式对应的锁，不存在时创建；锁对象本身常驻，避免键漂移。"""
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
            return lock

    def next_id(self, module: str) -> int:
        """给业务表分配下一个自增 id。"""
        rows = self.rows(module)
        return max((int(row.get("id", 0)) for row in rows), default=0) + 1

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
