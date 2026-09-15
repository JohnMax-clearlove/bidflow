"""面向宿主 Agent 的中文命令入口。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from .project import Project, INPUT_DIRS
from .utils import atomic_json, read_json, safe_name, utc_now, write_text


def _common(parser):
    parser.add_argument("--project", default=".", help="独立投标项目目录，默认当前目录")
    return parser


def _parser():
    parser = argparse.ArgumentParser(prog="bidflow", description="本地优先的投标文件编制工作流，语义任务交由当前Agent执行")
    parser.add_argument("--version", action="version", version=f"BidFlow {__version__} 待实标验收")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="创建独立投标项目")
    init.add_argument("name")
    init.add_argument("--path", help="项目目录，默认projects/项目名称")
    init.add_argument("--library", help="本地公司资料库位置")
    _common(sub.add_parser("status", help="查看并更新项目状态"))
    _common(sub.add_parser("next", help="查看可续接的下一步"))
    _common(sub.add_parser("doctor", help="检查运行依赖，不启动Word"))
    inp = _common(sub.add_parser("ingest", help="导入原件并建立可追溯分块"))
    inp.add_argument("source", nargs="?")
    inp.add_argument("--scan", action="store_true", help="扫描项目输入目录，按所在子目录导入")
    inp.add_argument("--category", default="招标文件")
    inp.add_argument("--ocr", action="store_true")
    search = _common(sub.add_parser("search", help="中文检索已导入资料"))
    search.add_argument("query")
    search.add_argument("--category", action="append")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--library", action="store_true", help="同时检索已配置的本地公司资料库")
    _common(sub.add_parser("reindex", help="从主记录重建全文索引"))
    ledger = _common(sub.add_parser("ledger", help="导入Excel或CSV业绩台账"))
    ledger.add_argument("source")
    ledger.add_argument("--mapping", help="列映射JSON文件")
    for name, help_text in [("match", "检查资格及证明材料匹配"), ("score", "计算证据支持分及潜在分"), ("audit", "检查资格、评分、正文及输出"), ("reports", "生成各阶段可读报告")]:
        _common(sub.add_parser(name, help=help_text))
    optimize = _common(sub.add_parser("staff", help="比较人员岗位配置"))
    optimize.add_argument("--config", required=True, help="岗位、候选及约束JSON")
    task = sub.add_parser("task", help="生成任务包或接收Agent结果").add_subparsers(dest="action", required=True)
    prepare = _common(task.add_parser("prepare", help="准备有明确来源和输出规范的任务"))
    prepare.add_argument("stage", choices=["analyze", "amendment", "evidence", "plan", "write", "review"])
    prepare.add_argument("--target", help="文件ID、规则ID或章节ID，具体依阶段而定")
    accept = _common(task.add_parser("accept", help="校验并接收任务结果"))
    accept.add_argument("--task", required=True)
    accept.add_argument("--result", required=True)
    accept.add_argument("--actor", required=True, help="实际执行Agent或人工标识")
    confirm = _common(sub.add_parser("confirm", help="记录用户对当前版本的明确确认"))
    confirm.add_argument("scope", choices=["rules", "selection", "brief", "plan", "draft", "assembly", "visual"])
    confirm.add_argument("--actor", required=True)
    confirm.add_argument("--notes", default="")
    data = sub.add_parser("data", help="导入人工整理的数据，不通过此入口改招标规则").add_subparsers(dest="action", required=True)
    show = _common(data.add_parser("show"))
    show.add_argument("collection")
    imp = _common(data.add_parser("import"))
    imp.add_argument("collection", choices=["facts", "settings", "staff", "history", "forms", "manual_checks"])
    imp.add_argument("source")
    imp.add_argument("--actor", required=True)
    form = _common(sub.add_parser("form", help="填写已核准的招标表单模板"))
    form.add_argument("id")
    build = _common(sub.add_parser("build", help="生成审阅稿或封存已确认成品"))
    build.add_argument("--mode", choices=["review", "ready"], default="review")
    build.add_argument("--split", action="store_true")
    build.add_argument("--no-render", action="store_true", help="仅生成未分页草稿，不作为验收完成")
    _common(sub.add_parser("verify", help="验证当前Word/PDF的页码和链接"))
    schemas = sub.add_parser("schemas", help="导出Agent结果的JSON Schema")
    schemas.add_argument("directory", nargs="?", default="schemas")
    library = sub.add_parser("library", help="初始化可长期复用的本地公司资料库")
    library.add_argument("path", nargs="?", default="company_library")
    connector = _common(sub.add_parser("connector", help="检索本地资料库或生成外部人工检索任务"))
    connector.add_argument("name", choices=["local_library", "dingtalk", "feishu", "contract_system"])
    connector.add_argument("query")
    connector.add_argument("--category", action="append")
    connector.add_argument("--limit", type=int, default=20)
    recover = _common(sub.add_parser("recover", help="在原写入进程已终止后恢复项目日志"))
    recover.add_argument("--pid", type=int, required=True, help="锁文件记录的旧进程号，必须已终止")
    return parser


def doctor() -> dict:
    modules = {name: bool(importlib.util.find_spec(name)) for name in ["pydantic", "docx", "pdfplumber", "pypdfium2", "pypdf", "openpyxl", "PIL", "rapidocr", "onnxruntime"]}
    word = None
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Word.Application\CLSID") as key:
                word = winreg.QueryValueEx(key, None)[0]
        except OSError:
            pass
    ocr_models = False
    if modules["rapidocr"]:
        spec = importlib.util.find_spec("rapidocr")
        root = Path(spec.origin).parent / "models" if spec and spec.origin else None
        ocr_models = bool(root and all((root / name).is_file() for name in ("PP-OCRv6_det_small.onnx", "ch_ppocr_mobile_v2.0_cls_mobile.onnx", "PP-OCRv6_rec_small.onnx")))
    return {"version": __version__, "python": sys.version.split()[0], "modules": modules, "pandoc": shutil.which("pandoc"), "word_registered": bool(word), "powershell": shutil.which("powershell") or shutil.which("pwsh"), "model_api_required": False, "ocr": "本地OCR及随包模型可用" if ocr_models and modules["onnxruntime"] else "可选OCR依赖或模型尚未就绪；扫描件将生成待核提示", "acceptance_status": "待实标验收"}


def init_library(path: str | Path) -> dict:
    from uuid import uuid4
    root = Path(path).resolve()
    control = root / ".bidflow"
    if (control / "project.json").exists():
        return {"library": str(root), "status": "已存在，保留原资料"}
    root.mkdir(parents=True, exist_ok=True)
    for directory in ["records", "cache", "history", "tasks"]:
        (control / directory).mkdir(parents=True, exist_ok=True)
    for directory in INPUT_DIRS[1:] + ["09_方法论", "10_历史章节", "11_业绩台账"]:
        (root / "01_输入文件" / directory).mkdir(parents=True, exist_ok=True)
    atomic_json(control / "project.json", {"schema_version": 1, "version": __version__, "id": str(uuid4()), "name": "公司公共资料库", "created_at": utc_now(), "company_library": None, "kind": "library", "acceptance_status": "资料适用性须按各项目重新核验"})
    write_text(root / "AGENTS.md", "# 公司公共资料库规则\n\n本目录维护公司证照、资质、人员证书、业绩台账和方法论。仅作本地检索与资料维护，不在此编制具体投标文件。原始资料不覆盖，更新文件另存并重新导入。台账事实不能代替正式证明。同名人员无稳定ID时保留歧义。采用的材料导入独立投标项目后核验，不因公共库已存在就视为本项目符合。所有资料及隐藏记录随库归档，不进入公开仓库。\n")
    write_text(root / ".gitignore", "*\n!.gitignore\n")
    return {"library": str(root), "status": "已创建", "next": "将资料放入分类目录，执行 bidflow ingest --scan --project 公司资料库位置"}


def _recover(path: str, expected_pid: int) -> dict:
    root = Path(path).resolve()
    lock = root / ".bidflow/write.lock"
    if lock.exists():
        pid = int(lock.read_text(encoding="ascii"))
        if pid != expected_pid:
            raise ValueError("指定进程号与当前锁不一致，未移除锁")
        alive = False
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                alive = True
                ctypes.windll.kernel32.CloseHandle(handle)
        else:
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                pass
        if alive:
            raise ValueError("持锁进程仍在运行，不能抢占")
        lock.unlink()
    project = Project(root)
    return {"status": "恢复检查完成", "project": project.meta["name"]}


def run(args) -> dict:
    from . import workflow
    if args.command == "init":
        path = Path(args.path) if args.path else Path("projects") / safe_name(args.name)
        library = args.library or (str(Path("company_library").resolve()) if Path("company_library/.bidflow/project.json").exists() else None)
        project = Project.create(path, args.name, library)
        return {"project": str(project.root), "status": "已创建", "next": "在Codex中打开此项目文件夹，放入招标文件后开始拆标"}
    if args.command == "doctor":
        return doctor()
    if args.command == "schemas":
        return workflow.export_schemas(args.directory)
    if args.command == "library":
        return init_library(args.path)
    if args.command == "recover":
        return _recover(args.project, args.pid)
    project = Project(args.project)
    if args.command == "status":
        workflow.sync(project)
        return project.refresh_status()
    if args.command == "next":
        return workflow.next_steps(project)
    if args.command == "connector":
        from dataclasses import asdict
        from .connectors import get_connector
        return {"connector": args.name, "results": [asdict(hit) for hit in get_connector(project, args.name).search(args.query, args.category, args.limit)]}
    if args.command in {"ingest", "search", "reindex", "ledger"}:
        from . import ingest
        if args.command == "ingest":
            if args.scan:
                mapping = {value: key for key, value in ingest.CATEGORY_DIRS.items() if key != "澄清补遗"}
                mapping.update({"08_其他补充资料": "其他补充资料", "09_方法论": "方法论", "10_历史章节": "历史章节", "11_业绩台账": "业绩台账"})
                results = []
                for path in sorted((project.root / "01_输入文件").rglob("*")):
                    if not path.is_file() or path.name.startswith("~$"):
                        continue
                    category = mapping.get(path.relative_to(project.root / "01_输入文件").parts[0], "其他补充资料")
                    known = next((f for f in project.load("files") if f.get("path") == path.relative_to(project.root).as_posix()), None)
                    if known:
                        category = known["category"]
                    if path.suffix.lower() in {".xlsx", ".csv"}:
                        results.append(ingest.import_ledger(project, path))
                    elif path.suffix.lower() in ingest.DOCUMENT_EXTENSIONS:
                        results.append(ingest.ingest(project, path, category, args.ocr))
                workflow.sync(project)
                project.refresh_status()
                return {"results": results}
            if not args.source:
                raise ValueError("需要source文件或--scan")
            result = ingest.ingest(project, args.source, args.category, args.ocr)
            workflow.sync(project)
            project.refresh_status()
            return result
        if args.command == "search":
            result = {"project": ingest.search(project, args.query, args.category, args.limit)}
            if args.library:
                library = project.meta.get("company_library")
                result["library"] = ingest.search(Project(library), args.query, args.category, args.limit) if library else {"status": "未配置公司资料库"}
            return result
        if args.command == "reindex":
            return ingest.reindex(project)
        return ingest.import_ledger(project, args.source, read_json(args.mapping, {}) if args.mapping else None)
    if args.command in {"match", "score", "audit", "reports", "staff"}:
        from . import matching
        workflow.sync(project)
        if args.command == "staff":
            return matching.optimize_staff(project, read_json(args.config, {}))
        return getattr(matching, "render_reports" if args.command == "reports" else args.command)(project)
    if args.command == "task":
        if args.action == "prepare":
            return workflow.prepare(project, args.stage, args.target)
        path = Path(args.result)
        if not path.is_absolute() and not path.exists():
            path = project.safe_path(path)
        return workflow.accept(project, args.task, path, args.actor)
    if args.command == "confirm":
        return workflow.confirm(project, args.scope, args.actor, args.notes)
    if args.command == "data":
        if args.action == "show":
            return {"collection": args.collection, "records": project.load(args.collection, {} if args.collection in {"facts", "settings"} else [])}
        value = read_json(args.source)
        if value is None:
            raise ValueError("数据文件不存在")
        if args.collection in {"facts", "settings"}:
            if not isinstance(value, dict):
                raise ValueError("facts/settings须为JSON对象")
            merged = project.load(args.collection, {})
            merged.update(value)
            value = merged
        else:
            if not isinstance(value, list) or any(not isinstance(row, dict) or not row.get("id") for row in value):
                raise ValueError("集合须为含唯一id的记录列表")
            existing = project.load(args.collection)
            by_id = {row["id"]: row for row in existing}
            by_id.update({row["id"]: row for row in value})
            value = list(by_id.values())
            for row in value:
                for field in ("path", "template_path", "output_path"):
                    if row.get(field):
                        project.safe_path(row[field])
        project.save(args.collection, value, reason=f"{args.actor}导入人工数据")
        workflow.sync(project)
        project.refresh_status()
        return {"collection": args.collection, "status": "已导入，关联确认及审核按新版本重新核对"}
    if args.command == "form":
        from .forms import fill_form
        return fill_form(project, args.id)
    if args.command in {"build", "verify"}:
        from .assembly import build, verify_output
        return build(project, args.mode, args.split, not args.no_render) if args.command == "build" else verify_output(project)
    raise ValueError("未知命令")


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    try:
        result = run(args)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2, default=str))
    except (ValueError, RuntimeError, OSError, ImportError, KeyError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc), "command": args.command}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
