"""外部资料接口边界；首版只完成本地资料库闭环。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .ingest import ingest, search
from .project import Project


@dataclass
class ConnectorHit:
    connector: str
    source_id: str
    title: str
    source: str
    status: str = "candidate"
    metadata: dict[str, Any] = field(default_factory=dict)


class Connector(Protocol):
    name: str

    def search(self, query: str, categories: list[str] | None = None, limit: int = 20) -> list[ConnectorHit]: ...

    def materialize(self, project: Project, source_id: str, category: str) -> dict: ...


class LocalLibraryConnector:
    """检索本地公共资料库；采用材料时复制到当前项目并重新建立来源。"""

    name = "local_library"

    def __init__(self, library: str | Path):
        self.library = Project(library)

    def search(self, query: str, categories: list[str] | None = None, limit: int = 20) -> list[ConnectorHit]:
        result = search(self.library, query, categories, limit)
        hits = []
        files = {row["id"]: row for row in self.library.load("files")}
        for row in result["results"]:
            file_id = row.get("file_id") or (row.get("id") if row.get("type") == "file" else None)
            file = files.get(file_id, {})
            if not file_id or not file.get("path"):
                continue
            hits.append(ConnectorHit(self.name, file_id, file.get("original_name", file_id), file["path"], metadata={"category": file.get("category"), "locator": row.get("locator"), "page": row.get("page"), "text": row.get("text", "")}))
        return hits

    def materialize(self, project: Project, source_id: str, category: str) -> dict:
        file = next((row for row in self.library.load("files") if row["id"] == source_id), None)
        if not file:
            raise ValueError("本地资料库中不存在指定文件")
        return ingest(project, self.library.safe_path(file["path"]), category)


class ManualExternalConnector:
    """钉钉、飞书及合同系统的首版接口占位，不虚构已接通能力。"""

    def __init__(self, name: str):
        if name not in {"dingtalk", "feishu", "contract_system"}:
            raise ValueError("未知外部资料来源")
        self.name = name

    def search(self, query: str, categories: list[str] | None = None, limit: int = 20) -> list[ConnectorHit]:
        return [ConnectorHit(self.name, "manual-request", f"待人工检索：{query}", "外部系统尚未接通", status="manual_required", metadata={"categories": categories or [], "limit": limit, "next": "人工下载后放入当前项目对应输入目录，再运行bidflow ingest --scan"})]

    def materialize(self, project: Project, source_id: str, category: str) -> dict:
        raise RuntimeError("首版未连接外部系统；请将已下载原件放入当前项目，再从本地导入")


def get_connector(project: Project, name: str) -> Connector:
    if name == "local_library":
        library = project.meta.get("company_library")
        if not library:
            raise ValueError("当前项目尚未配置本地公司资料库")
        return LocalLibraryConnector(library)
    return ManualExternalConnector(name)
