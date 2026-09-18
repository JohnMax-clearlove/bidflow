"""项目主记录、修订日志和可恢复的原子提交。"""

from __future__ import annotations

import copy
import json
import os
import uuid
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any

from bidflow import __version__
from bidflow.utils import atomic_json, json_hash, read_json, sha256_file, utc_now, write_text

STAGES = ["01_输入文件", "02_招标拆解", "03_资料匹配", "04_技术标策划", "05_投标文件编制", "06_审核检查", "07_最终输出"]
INPUT_DIRS = ["01_招标文件", "02_公司证照", "03_公司资质", "04_荣誉奖项", "05_人员证书", "06_企业业绩证明", "07_人员业绩证明", "08_其他补充资料"]
COLLECTIONS = {"files", "blocks", "rules", "evidence", "responses", "staff", "history", "plans", "sections", "reviews", "forms", "tasks", "confirmations", "manual_checks", "issues", "positions", "facts", "settings", "assembly", "analysis_coverage", "ledger_imports", "match_results", "staff_solutions", "final_reviews", "evidence_coverage", "ready_history"}


class Project:
    def __init__(self, path: str | Path):
        self.root = Path(path).resolve()
        self.control_dir = self.root / ".bidflow"
        self.data_dir = self.control_dir / "records"
        self.cache_dir = self.control_dir / "cache"
        if not (self.control_dir / "project.json").exists():
            raise ValueError(f"尚未初始化投标项目：{self.root}；请先运行 bidflow init")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        meta = self.meta
        if meta.get("schema_version") != 1:
            raise ValueError("项目数据版本不受支持，请使用创建项目时的工具版本或执行经验证的迁移")
        if (self.control_dir / "pending.json").exists():
            with self._lock():
                self._recover()

    @classmethod
    def create(cls, path: str | Path, name: str, company_library: str | Path | None = None) -> Project:
        root = Path(path).resolve()
        if root.exists() and any(root.iterdir()):
            raise ValueError("目标目录不是空目录；为保护已有资料，请选择新的项目目录")
        root.mkdir(parents=True, exist_ok=True)
        for directory in STAGES + [f"01_输入文件/{part}" for part in INPUT_DIRS] + [".bidflow/records", ".bidflow/cache", ".bidflow/history", ".bidflow/tasks"]:
            (root / directory).mkdir(parents=True, exist_ok=True)
        atomic_json(root / ".bidflow/project.json", {"schema_version": 1, "version": __version__, "id": str(uuid.uuid4()), "name": name, "created_at": utc_now(), "company_library": str(Path(company_library).resolve()) if company_library else None, "acceptance_status": "待实标验收"})
        write_text(root / "AGENTS.md", files("bidflow").joinpath("assets/project_agents.md").read_text(encoding="utf-8"))
        write_text(root / ".gitignore", "*\n!.gitignore\n")
        project = cls(root)
        project.commit({"facts": {"project_name": name}, "settings": {"anonymous": False, "allow_indices": True, "volumes": [], "max_review_rounds": 2, "context_budget": 16000}, "manual_checks": [{"id": "MC001", "title": "签字盖章及授权有效性", "status": "pending", "actor": "", "notes": "人工确认，程序不代签"}, {"id": "MC002", "title": "投标保证金及提交手续", "status": "pending", "actor": "", "notes": "按本项目招标要求核对适用性"}, {"id": "MC003", "title": "全文名称日期金额复核", "status": "pending", "actor": "", "notes": "在内容定稿及实际分页后人工复核"}]}, reason="创建项目")
        project.refresh_status()
        return project

    @property
    def meta(self) -> dict:
        return read_json(self.control_dir / "project.json", {})

    def safe_path(self, relative: str | Path) -> Path:
        value = Path(relative)
        if value.is_absolute():
            raise ValueError("项目记录只能使用项目内相对路径")
        target = (self.root / value).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("路径超出当前投标项目")
        return target

    def _record_path(self, name: str) -> Path:
        if not name or not name.replace("_", "").isalnum():
            raise ValueError("无效集合名")
        return self.data_dir / f"{name}.json"

    def load(self, name: str, default: Any = None) -> Any:
        return read_json(self._record_path(name), [] if default is None else default)

    @contextmanager
    def _lock(self):
        path = self.control_dir / "write.lock"
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise RuntimeError("此项目有尚未释放的写入锁；先确认其他操作已结束，再执行 bidflow recover") from None
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            os.close(descriptor)
            yield
        finally:
            path.unlink(missing_ok=True)

    def _recover(self) -> None:
        journal = self.control_dir / "pending.json"
        pending = read_json(journal)
        if not pending:
            return
        for name, value in pending["changes"].items():
            atomic_json(self._record_path(name), value)
        atomic_json(self.control_dir / "history" / f"{pending['id']}.json", pending)
        journal.unlink(missing_ok=True)

    def commit(self, changes: dict[str, Any], reason: str = "", expected: dict[str, str] | None = None) -> None:
        changes = copy.deepcopy(changes)
        for name, value in changes.items():
            self._record_path(name)
            if isinstance(value, list):
                ids = [row["id"] for row in value if isinstance(row, dict) and row.get("id")]
                if len(ids) != len(set(ids)):
                    raise ValueError(f"{name} 中存在重复编号")
        with self._lock():
            self._recover()
            for name, digest in (expected or {}).items():
                if self.fingerprint([name]) != digest:
                    raise RuntimeError(f"{name} 已被其他操作更新，本次提交未覆盖新结果")
            before = {name: self.load(name) for name in changes}
            if all(before[name] == value for name, value in changes.items()):
                return
            entry = {"id": uuid.uuid4().hex, "at": utc_now(), "reason": reason, "before": before, "changes": changes}
            atomic_json(self.control_dir / "pending.json", entry)
            self._recover()

    def save(self, name: str, value: Any, reason: str = "") -> None:
        self.commit({name: value}, reason=reason)

    def fingerprint(self, names: list[str]) -> str:
        values = {name: self.load(name, {} if name in {"facts", "settings", "assembly"} else []) for name in sorted(names)}
        actual: dict[str, str] = {}
        for value in values.values():
            rows = value if isinstance(value, list) else [value]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                for field in ("path", "template_path", "output_path"):
                    if row.get(field):
                        try:
                            path = self.safe_path(row[field])
                            actual[row[field]] = sha256_file(path) if path.is_file() else "missing"
                        except (ValueError, OSError):
                            actual[str(row[field])] = "unavailable"
        return json_hash({"records": values, "actual_files": actual})

    def refresh_status(self) -> dict:
        tasks = self.load("tasks")
        rules = self.load("rules")
        issues = self.load("issues")
        pending = [task for task in tasks if task.get("status") in {"pending", "conflict", "stale"}]
        confirmations = self.load("confirmations")
        facts = self.load("facts", {})
        status = {"project": self.meta["name"], "acceptance_status": self.meta["acceptance_status"], "files": len(self.load("files")), "rules": len([r for r in rules if r.get("status") != "retired"]), "evidence": len(self.load("evidence")), "sections": len(self.load("sections")), "pending_tasks": [{"id": task["id"], "stage": task["stage"], "status": task["status"]} for task in pending], "open_issues": len([issue for issue in issues if issue.get("status", "open") == "open"]), "confirmations": [{"scope": c["scope"], "actor": c["actor"], "at": c["at"]} for c in confirmations], "facts": facts}
        final_reviews = self.load("final_reviews", [])
        final_lines, final_summary = [], []
        for row in final_reviews:
            computed, lane_text = row.get("status", "pending"), ""
            try:
                from .final_review import evaluate as evaluate_final_review
                state = evaluate_final_review(self, row)
                computed = state["status"]
                lane_text = "；" + "，".join(f"{lane} rev{state['lanes'][lane]['revision']}" for lane in ("human", "agent"))
                final_lines.append(f"- {row['id']}（{row['stage']}，writer {row['writer']}）：{computed}{lane_text}；{state['next']}")
            except Exception:
                # 实时复算失败时不得回退展示旧缓存状态，避免把已失效的通过当成现状。
                computed = "blocked"
                final_lines.append(f"- {row['id']}（{row['stage']}）：blocked（实时状态复算失败，需人工核查记录完整性）")
            final_summary.append({"id": row["id"], "stage": row["stage"], "status": computed})
        status["final_reviews"] = final_summary
        lines = [f"# {self.meta['name']} 项目状态", "", f"版本状态：{self.meta['acceptance_status']}", "", f"输入文件 {status['files']} 份；招标要求 {status['rules']} 项；证明材料 {status['evidence']} 份；正文章节 {status['sections']} 章。", "", "## 当前待办", ""]
        lines += [f"- {t['id']}：{t['stage']}（{t['status']}）" for t in pending] or ["- 运行 `bidflow next --project .` 查看下一步。"]
        lines += ["", "## 成品双审", ""]
        lines += final_lines or ["- 尚无成品双审记录。组卷完成后运行 `bidflow final-review start --stage content --assembly --writer 编制者`，由人工与Agent独立核查。"]
        lines += ["", "## 已记录确认", ""]
        lines += [f"- {c['scope']}：{c['actor']}，{c['at']}；确认只对其记录版本有效。" for c in confirmations] or ["- 尚无人工确认。"]
        lines += ["", "## 人工处理事项", ""]
        lines += [f"- {c['title']}：{c['status']} {c.get('notes', '')}" for c in self.load("manual_checks")]
        lines += ["", "## 资料保护", "", "项目迁移时复制整个文件夹，包括 .bidflow。仅 cache 为可重建缓存；原件、records、tasks、history 必须保留。", ""]
        write_text(self.root / "项目状态.md", "\n".join(lines))
        return status
