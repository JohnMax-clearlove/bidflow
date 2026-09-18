"""工作流审计 S1-S4 回归及证据覆盖/发现闭环/人工版本/报告与ready历史测试。

全部使用虚构公司与合成文本，不包含真实项目资料。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bidflow import workflow
from bidflow.ingest import ingest
from bidflow.matching import _context, _open_findings, audit, reports_status, score
from bidflow.project import Project
from bidflow.utils import sha256_file, write_text
from bidflow.workflow import accept, confirm, coverage, prepare


def make_project(tmp_path: Path, name: str = "整改回归合成项目") -> Project:
    """合成拆标：Q001、T001（两子要求）、R001，规则已确认。"""
    project = Project.create(tmp_path / name, name)
    tender = tmp_path / f"{name}_招标.md"
    write_text(tender, "资格要求：投标人具有有效法人证照。技术评分：工作方案满10分，须说明工作步骤和质量复核。必响应：项目负责人每月至少到岗一次。")
    imported = ingest(project, tender, "招标文件")
    block = project.load("blocks")[0]
    task = prepare(project, "analyze")["created"][0]["id"]

    def source(quote: str) -> list[dict]:
        return [{"file_id": imported["file_id"], "block_id": block["id"], "quote": quote, "page": block.get("page")}]

    accept(project, task, {
        "rules": [
            {"kind": "qualification", "title": "法人证照", "text": "投标人具有有效法人证照", "sources": source("投标人具有有效法人证照"), "criteria": {"proof_mode": "evidence"}},
            {"kind": "technical", "title": "工作方案", "text": "工作方案满10分，须说明工作步骤和质量复核", "sources": source("工作方案满10分，须说明工作步骤和质量复核"), "max_score": 10, "subrequirements": ["工作步骤", "质量复核"]},
            {"kind": "requirement", "title": "到岗要求", "text": "项目负责人每月至少到岗一次", "sources": source("项目负责人每月至少到岗一次"), "criteria": {"proof_mode": "either"}},
        ],
        "covered_block_ids": [block["id"]],
        "facts": {"company_name": "合成公司", "deadline": "2026-12-31", "service_period": "60日", "price": "10000"},
        "settings": {"anonymous": False, "allow_indices": True, "expected_total_score": 10},
        "conflicts": [], "retire_rule_ids": [],
    }, "拆标Agent")
    confirm(project, "rules", "规则确认人", attest_human=True)
    return project


def formal_brief() -> dict:
    return {"questions": ["文种用途", "发文主体", "主要读者", "决策目标", "正文范围", "依据来源"],
            "document_type": "技术方案", "purpose": "响应", "author": "合成公司", "audience": "评委",
            "decision_goal": "覆盖", "body_scope": "方案", "sources": ["招标"], "structure": ["步骤"],
            "length": "短", "tone": "正式"}


def write_section(project: Project, rule_ids: list[str], markdown: str = "# 方案\n\n## 工作步骤\n\n每月到岗。\n\n## 质量复核\n\n三级复核。") -> None:
    plan = prepare(project, "plan")["created"][0]["id"]
    accept(project, plan, {"plans": [{"id": "SEC001", "title": "方案", "rule_ids": rule_ids, "content_points": ["工作步骤"],
                                      "fact_refs": [], "method_refs": [], "visuals": []}]}, "策划Agent")
    facts = project.load("facts", {})
    facts["formal_brief"] = formal_brief()
    project.save("facts", facts, reason="记录正式文本沟通")
    confirm(project, "brief", "沟通确认人", attest_human=True)
    confirm(project, "plan", "策划确认人", attest_human=True)
    task = prepare(project, "write", "SEC001")["created"][0]["id"]
    accept(project, task, {"section_id": "SEC001", "writer_id": "writer", "markdown": markdown,
                           "covered_subrequirements": {"T001": ["工作步骤", "质量复核"]}}, "writer")


def review_pass(project: Project, status: str = "pass", findings: list[dict] | None = None, resolutions: list[dict] | None = None) -> tuple[dict, str]:
    prepared = prepare(project, "review", "SEC001")
    task = prepared["created"][0]["id"] if prepared["created"] else prepared["reused"][0]
    result = accept(project, task, {"section_id": "SEC001", "writer_id": "writer", "reviewer_id": "reviewer",
                                    "findings": findings or [], "resolutions": resolutions or [],
                                    "scores": [{"rule_id": "T001", "score": 9, "reason": "覆盖"}],
                                    "covered_subrequirements": {"T001": ["工作步骤", "质量复核"]}, "status": status}, "reviewer")
    return result, task


def test_s1_t_plus_r_review_is_current_and_rule_change_invalidates_both_sides(tmp_path):
    project = make_project(tmp_path)
    write_section(project, ["T001", "R001"])
    review_pass(project)
    confirm(project, "draft", "正文定稿确认人", attest_human=True)
    stored = project.load("reviews")[0]["rule_revisions"]
    assert stored == {"R001": 1, "T001": 1}
    first = audit(project)
    assert not any(issue["category"] == "review" and "尚无有效" in issue["message"] for issue in first["issues"])
    assert [row["rule_id"] for row in score(project)["technical_simulations"]] == ["T001"]

    rules = project.load("rules")
    next(row for row in rules if row["id"] == "R001")["revision"] = 2
    project.save("rules", rules, reason="合成测试：修改T+R章节相关规则")
    second = audit(project)
    assert any(issue["category"] == "review" and "尚无有效" in issue["message"] for issue in second["issues"])
    assert score(project)["technical_simulations"] == [], "规则改动后audit与score必须同时失效"
    assert len(project.load("reviews")) == 1, "旧审核记录必须保留，不能被改写"


def test_s3_open_findings_survive_pass_round_and_close_only_with_resolutions(tmp_path):
    project = make_project(tmp_path)
    settings = project.load("settings")
    settings["max_review_rounds"] = 3
    project.save("settings", settings, reason="合成测试：允许三轮复核")
    write_section(project, ["T001"])
    review_pass(project, status="revise", findings=[{"rule_id": "T001", "severity": "error", "message": "缺少质量复核记录证据",
                                                       "suggestion": "补充记录", "location": "正文"}])
    path = project.safe_path(project.load("sections")[0]["path"])
    write_text(path, path.read_text(encoding="utf-8") + "\n\n补充：质量复核留痕见附表。\n")
    confirm(project, "draft", "正文定稿确认人", attest_human=True)
    finding_id = project.load("reviews")[0]["findings"][0]["finding_id"]
    assert finding_id

    _, second_task = review_pass(project, status="pass")
    import json
    context = json.loads(project.safe_path(f".bidflow/tasks/{second_task}/context.json").read_text(encoding="utf-8"))["context"]
    assert any(row.get("finding_id") == finding_id and row.get("state") == "open" for row in context.get("prior_open_findings", [])), "新一轮任务包必须携带prior_open_findings"
    state = audit(project)
    assert any("缺少质量复核记录证据" in issue["message"] for issue in state["issues"])
    assert any(issue["category"] == "review" and issue["severity"] == "error" for issue in state["issues"])
    assert score(project)["technical_simulations"] == [], "存在未关闭error时不得计入模拟分"

    # extra里的status=resolved不具备关闭效力，仍按未关闭汇总。
    ctx = _context(project)
    legacy = {"id": "REVX", "section_id": "SEC001", "round": 9, "findings": [{"finding_id": "FND-X", "rule_id": "T001", "severity": "warning",
                                                                               "message": "旧版发现", "suggestion": "处理", "location": "正文", "status": "resolved"}]}
    ctx["reviews"] = [*ctx["reviews"], legacy]
    assert "FND-X" in [row["finding_id"] for row in _open_findings(ctx, "SEC001")]

    review_pass(project, status="pass", resolutions=[{"finding_id": finding_id, "basis": "已补充质量复核留痕并在附表定位",
                                                       "evidence_ids": [], "location": "正文末附表"}])
    final = audit(project)
    assert not any("缺少质量复核记录证据" in issue["message"] for issue in final["issues"])
    assert [row["rule_id"] for row in score(project)["technical_simulations"]] == ["T001"]
    assert len(project.load("reviews")) == 3, "历轮审核记录必须保留"


def test_s2_evidence_coverage_declaration_and_reuse(tmp_path):
    project = make_project(tmp_path)
    proof = project.safe_path("01_输入文件/02_公司证照/合成证明.md")
    write_text(proof, "\n\n".join(f"合成证照第{i}段：主体名称合成公司。" for i in range(1, 5)))
    imported = ingest(project, proof, "公司证照")
    blocks = [row for row in project.load("blocks") if row["file_id"] == imported["file_id"]]
    assert len(blocks) == 4
    task = prepare(project, "evidence", imported["file_id"])["created"][0]["id"]

    with pytest.raises(ValueError, match="covered_block_ids"):
        accept(project, task, {"evidence": [], "responses": []}, "证据Agent")
    with pytest.raises(ValueError, match="完全一致"):
        accept(project, task, {"evidence": [], "responses": [], "no_evidence_reason": "合成测试", "covered_block_ids": [blocks[0]["id"]]}, "证据Agent")
    with pytest.raises(ValueError, match="重复分块"):
        accept(project, task, {"evidence": [], "responses": [], "no_evidence_reason": "合成测试", "covered_block_ids": [blocks[0]["id"], blocks[0]["id"], blocks[1]["id"], blocks[2]["id"]]}, "证据Agent")
    with pytest.raises(ValueError, match="无有效证据的理由"):
        accept(project, task, {"evidence": [], "responses": [], "covered_block_ids": [block["id"] for block in blocks]}, "证据Agent")

    declared = [block["id"] for block in blocks]
    result = accept(project, task, {"evidence": [], "responses": [], "covered_block_ids": declared, "no_evidence_reason": "四段全为空白模板，无有效证明内容"}, "证据Agent")
    assert result["status"] == "已接收"
    records = project.load("evidence_coverage")
    assert len(records) == 4 and all(row["task_id"] == task and row["block_hash"] for row in records)
    again = prepare(project, "evidence", imported["file_id"])
    assert again["created"] == [] and again["reused"] == [task], "有当前覆盖的旧结果才可复用"
    state = audit(project)
    assert not any(issue["category"] == "evidence" and "覆盖" in issue["message"] for issue in state["issues"])

    # 旧accepted但无覆盖：不得静默复用，必须重新生成该文件的分块任务。
    legacy = project.safe_path("01_输入文件/02_公司证照/旧任务证明.md")
    write_text(legacy, "旧任务证明内容")
    project.commit({"files": [*project.load("files"), {"id": "DOC_OLD", "path": legacy.relative_to(project.root).as_posix(), "sha256": sha256_file(legacy), "category": "公司证照", "page_count": 1}],
                    "blocks": [*project.load("blocks"), {"id": "B_OLD", "file_id": "DOC_OLD", "text": "旧任务证明内容", "page": 1, "quality": "ok"}]}, reason="合成测试：旧任务材料")
    pending = prepare(project, "evidence", "DOC_OLD")["created"][0]["id"]
    tasks = project.load("tasks")
    next(row for row in tasks if row["id"] == pending).update(status="accepted", result_hash="legacy", accepted_at="2026-01-01T00:00:00+00:00")
    project.save("tasks", tasks, reason="合成测试：构造无覆盖旧accepted任务")
    retake = prepare(project, "evidence", "DOC_OLD")
    assert pending not in retake["reused"], "旧accepted但无当前覆盖不得复用"
    assert retake["created"], "无覆盖分块必须重新生成任务"


def test_evidence_pages_limited_to_task_blocks(tmp_path):
    project = make_project(tmp_path)
    proof = project.safe_path("01_输入文件/02_公司证照/分页证明.md")
    blocks = [{"id": f"BP{number}", "file_id": "DOC_PAGES", "text": f"合成第{number}页" + "甲" * 180, "page": number, "quality": "ok"} for number in range(1, 5)]
    write_text(proof, "合成分页证明")
    project.commit({"files": [*project.load("files"), {"id": "DOC_PAGES", "path": proof.relative_to(project.root).as_posix(), "sha256": sha256_file(proof), "category": "公司证照", "page_count": 4}],
                    "blocks": [*project.load("blocks"), *blocks]}, reason="合成测试：构造分页证明")
    settings = project.load("settings")
    settings["context_budget"] = 3000
    project.save("settings", settings, reason="合成测试：小预算拆任务")
    tasks = prepare(project, "evidence", "DOC_PAGES")["created"]
    assert len(tasks) >= 2, "小预算下每个分块单独成任务"
    first = tasks[0]["id"]
    record = next(row for row in project.load("tasks") if row["id"] == first)
    assigned = record["dependencies"]["collections"]["blocks"]
    assert assigned and all(row["page"] == 1 for row in blocks if row["id"] in assigned)
    with pytest.raises(ValueError, match="任务未核对"):
        accept(project, first, {"evidence": [{"file_id": "DOC_PAGES", "title": "证明", "pages": [4], "facts": {"company_name": "合成公司"}, "verification": "verified"}],
                                "responses": [], "covered_block_ids": assigned}, "证据Agent")
    accepted = accept(project, first, {"evidence": [{"file_id": "DOC_PAGES", "title": "证明", "pages": [1], "facts": {"company_name": "合成公司"}, "verification": "verified"}],
                                       "responses": [], "covered_block_ids": assigned}, "证据Agent")
    assert accepted["status"] == "已接收"

    # 无物理页映射时不得猜页码。
    nopage = project.safe_path("01_输入文件/02_公司证照/无页映射证明.md")
    write_text(nopage, "无页映射证明")
    project.commit({"files": [*project.load("files"), {"id": "DOC_NOPAGE", "path": nopage.relative_to(project.root).as_posix(), "sha256": sha256_file(nopage), "category": "公司证照", "page_count": 1}],
                    "blocks": [*project.load("blocks"), {"id": "B_NOPAGE", "file_id": "DOC_NOPAGE", "text": "无页映射证明", "quality": "ok"}]}, reason="合成测试：无页映射材料")
    second_task = prepare(project, "evidence", "DOC_NOPAGE")["created"][0]["id"]
    second_record = next(row for row in project.load("tasks") if row["id"] == second_task)
    second_assigned = second_record["dependencies"]["collections"]["blocks"]
    with pytest.raises(ValueError, match="物理页映射"):
        accept(project, second_task, {"evidence": [{"file_id": "DOC_NOPAGE", "title": "证明", "pages": [1], "facts": {"company_name": "合成公司"}, "verification": "verified"}],
                                      "responses": [], "covered_block_ids": second_assigned}, "证据Agent")


def test_adopted_evidence_without_coverage_cannot_support_score(tmp_path):
    project = make_project(tmp_path)
    proof = project.safe_path("01_输入文件/02_公司证照/未覆盖证明.md")
    write_text(proof, "合成证照内容")
    project.commit({"files": [*project.load("files"), {"id": "DOC_RAW", "path": proof.relative_to(project.root).as_posix(), "sha256": sha256_file(proof), "category": "公司证照", "page_count": 1}],
                    "blocks": [*project.load("blocks"), {"id": "B_RAW", "file_id": "DOC_RAW", "text": "合成证照内容", "page": 1, "quality": "ok"}],
                    "evidence": [{"id": "E_RAW", "title": "未覆盖证明", "file_id": "DOC_RAW", "file_sha256": sha256_file(proof), "pages": [1], "facts": {"company_name": "合成公司"}, "verification": "verified", "verified_by": "人工"}],
                    "responses": [{"id": "RESP_RAW", "rule_id": "Q001", "evidence_ids": ["E_RAW"], "section_ids": [], "status": "supported", "rationale": "声称覆盖", "missing": [], "rule_revision": 1, "covered_subrequirements": []}]}, reason="合成测试：未覆盖证据")
    state = audit(project)
    assert any(issue["category"] == "evidence" and "覆盖核查" in issue["message"] for issue in state["issues"])
    assert not [row for row in state["matching"]["rules"] if row["rule_id"] == "Q001"][0]["supported"]
    assert score(project)["business_supported_score"] == 0


def test_idle_uncovered_evidence_only_warns(tmp_path):
    project = make_project(tmp_path)
    proof = project.safe_path("01_输入文件/02_公司证照/闲置证明.md")
    write_text(proof, "闲置材料")
    project.commit({"files": [*project.load("files"), {"id": "DOC_IDLE", "path": proof.relative_to(project.root).as_posix(), "sha256": sha256_file(proof), "category": "公司证照", "page_count": 1}],
                    "blocks": [*project.load("blocks"), {"id": "B_IDLE", "file_id": "DOC_IDLE", "text": "闲置材料", "page": 1, "quality": "ok"}],
                    "evidence": [{"id": "E_IDLE", "title": "闲置证明", "file_id": "DOC_IDLE", "file_sha256": sha256_file(proof), "pages": [1], "facts": {"company_name": "合成公司"}, "verification": "verified", "verified_by": "人工"}]}, reason="合成测试：闲置材料")
    state = audit(project)
    idle = [issue for issue in state["issues"] if issue["category"] == "evidence_idle"]
    assert len(idle) == 1 and "仅作提示" in idle[0]["message"]
    assert not any(issue["category"] == "evidence" for issue in state["issues"])


def test_s4_manual_confirmation_requires_attestation_and_version_binding(tmp_path):
    project = make_project(tmp_path)
    with pytest.raises(ValueError, match="attest-human"):
        confirm(project, "draft", "AGENT/Codex，非人工")
    # 程序不靠actor字符串识别身份：显式声明后录入的是human簿记，真实性由人和主Agent负责。
    record = confirm(project, "rules", "AGENT/Codex，非人工", attest_human=True)
    assert record["actor_kind"] == "human" and record["attestation"] == "human"

    # 旧式记录缺少人工声明与版本绑定，不得视为已确认。
    project.save("manual_checks", [{"id": "MC003", "title": "全文名称日期金额复核", "status": "confirmed", "actor": "auto-agent"}], reason="合成测试：旧式记录")
    state = audit(project)
    mc3 = [issue["message"] for issue in state["issues"] if issue["category"] == "manual"]
    assert any("缺少显式人工声明" in message for message in mc3)
    assert any("未绑定输入版本" in message for message in mc3)

    workflow.import_manual_checks(project, [{"id": "MC003", "title": "全文名称日期金额复核", "notes": "合成人工复核"}], "用户本人", attest_human=True)
    assert not any(issue["category"] == "manual" and issue.get("refs") == ["MC003"] for issue in audit(project)["issues"])

    # 改稿件（正文文件被人工修改）后旧确认失效。
    section_path = project.safe_path("05_投标文件编制/SEC001.md")
    write_text(section_path, "# 合成正文\n初版。\n")
    project.save("sections", [{"id": "SEC001", "title": "合成正文", "path": section_path.relative_to(project.root).as_posix(), "sha256": sha256_file(section_path), "rule_ids": [], "status": "confirmed", "writer_id": "writer"}], reason="合成测试：建立章节")
    workflow.import_manual_checks(project, [{"id": "MC003", "title": "全文名称日期金额复核"}], "用户本人", attest_human=True)
    assert not any(issue["category"] == "manual" and issue.get("refs") == ["MC003"] for issue in audit(project)["issues"])
    write_text(section_path, "# 合成正文\n人工改动后的正文。\n")
    changed_draft = audit(project)
    assert any(issue["category"] == "manual" and issue.get("refs") == ["MC003"] and "输入版本已变化" in issue["message"] for issue in changed_draft["issues"]), "改稿后旧人工确认必须失效"

    facts = project.load("facts", {})
    facts["price"] = "99999"
    project.save("facts", facts, reason="合成测试：改价")
    changed = audit(project)
    assert any(issue["category"] == "manual" and issue.get("refs") == ["MC003"] and "输入版本已变化" in issue["message"] for issue in changed["issues"]), "改价后旧人工确认必须失效"


def test_manual_product_binding_detects_tampering(tmp_path):
    project = make_project(tmp_path)
    output = project.safe_path("06_审核检查/组卷预览/RUNX/投标文件.docx")
    output.parent.mkdir(parents=True, exist_ok=True)
    write_text(output, "合成成品（测试用占位字节）")
    project.save("assembly", {"run_id": "RUNX", "rendered": True, "outputs": [{"volume": "投标文件", "docx": output.relative_to(project.root).as_posix(), "pdf": None}],
                              "input_fingerprint": workflow.scope_fingerprint(project, "assembly")}, reason="合成测试：构造成品")
    workflow.import_manual_checks(project, [{"id": "MC001", "title": "签字盖章及授权有效性", "notes": "合成签章复核"}], "用户本人", attest_human=True)
    assert not any(issue["category"] == "manual" and issue.get("refs") == ["MC001"] for issue in audit(project)["issues"])
    write_text(output, "被人工改动的合成成品")
    problems = [issue["message"] for issue in audit(project)["issues"] if issue["category"] == "manual" and issue.get("refs") == ["MC001"]]
    assert any("已变化" in message for message in problems), "成品被改动后旧签章确认必须失效"


def test_reports_have_batch_fingerprint_and_archive(tmp_path):
    from bidflow.matching import render_reports

    project = make_project(tmp_path)
    first = render_reports(project)
    assert first["batch_id"] and first["input_fingerprint"]
    manifest_path = project.safe_path("06_审核检查/报告清单.json")
    import json
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["current_batch"] == first["batch_id"] and len(manifest["batches"]) == 1
    batch = manifest["batches"][0]
    assert batch["input_fingerprint"] == first["input_fingerprint"]
    assert batch["files"] and project.safe_path("06_审核检查/报告历史/" + first["batch_id"] + "/审核清单.md").is_file()

    facts = project.load("facts", {})
    facts["service_period"] = "90日"
    project.save("facts", facts, reason="合成测试：报告输入变化")
    status = reports_status(project)
    assert status["stale"] is True and status["current"] == first["batch_id"]
    tampered = project.safe_path("06_审核检查/审核清单.md")
    write_text(tampered, tampered.read_text(encoding="utf-8") + "\n人工追加")
    status = reports_status(project)
    assert "06_审核检查/审核清单.md" in status["modified"]

    second = render_reports(project)
    assert second["batch_id"] != first["batch_id"]
    assert second["input_fingerprint"] != first["input_fingerprint"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [row["id"] for row in manifest["batches"]] == [first["batch_id"], second["batch_id"]]
    assert manifest["batches"][1]["supersedes"] == first["batch_id"]
    assert manifest["batches"][0]["files"] == batch["files"], "旧批次哈希必须保留"
    warnings = [issue for issue in audit(project)["issues"] if issue["category"] == "report"]
    assert not warnings, "刚重建的报告不应显示为过期或改动"
    state = audit(project)
    assert not [issue for issue in state["issues"] if issue["category"] == "evidence_idle"]
    assert coverage(project)["complete"]


def test_ready_history_survives_later_review_build(tmp_path, monkeypatch):
    from docx import Document
    from pypdf import PdfWriter

    from bidflow import assembly as assembly_module
    from bidflow.assembly import assembly_fingerprint, build

    project = Project.create(tmp_path / "ready", "ready历史合成测试")
    docx_path = project.safe_path("06_审核检查/组卷预览/RUNY/投标文件.docx")
    pdf_path = project.safe_path("06_审核检查/组卷预览/RUNY/投标文件.pdf")
    docx_path.parent.mkdir(parents=True, exist_ok=True)
    document = Document()
    document.add_heading("合成待签章成品", 0)
    document.save(docx_path)
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.write(str(pdf_path))
    section_path = project.safe_path("05_投标文件编制/SEC001.md")
    write_text(section_path, "# 合成正文\n组卷测试。\n")
    project.commit({
        "sections": [{"id": "SEC001", "title": "合成正文", "path": section_path.relative_to(project.root).as_posix(), "sha256": sha256_file(section_path), "rule_ids": [], "status": "confirmed", "writer_id": "writer"}],
        "assembly": {"run_id": "RUNY", "rendered": True, "errors": [], "outputs": [
            {"volume": "投标文件", "docx": docx_path.relative_to(project.root).as_posix(), "pdf": pdf_path.relative_to(project.root).as_posix(),
             "page_count": 1, "hashes": {"docx": sha256_file(docx_path), "pdf": sha256_file(pdf_path)},
             "bookmarks": [], "expected_links": [], "crossrefs": [], "fields": []}]},
    }, reason="合成测试：构造待签章成品")
    assembly = project.load("assembly")
    assembly["input_fingerprint"] = assembly_fingerprint(project)
    project.save("assembly", assembly, reason="合成测试：绑定组卷输入指纹")

    monkeypatch.setattr(assembly_module, "_confirmed", lambda project, scope, fingerprint: True)
    monkeypatch.setattr(assembly_module, "verify_output", lambda project: {"status": "passed", "errors": [], "warnings": [], "volumes": []})
    monkeypatch.setattr("bidflow.workflow.is_confirmed", lambda project, scope: True)
    monkeypatch.setattr("bidflow.matching.audit", lambda project: {"status": "review_required", "issues": [], "matching": {}, "score": {}})
    monkeypatch.setattr("bidflow.final_review.assembly_gate", lambda project: {"ok": True, "review_id": "FR-SYNTH"})
    published = build(project, mode="ready")
    assert published["status"] == "ready_for_signature"
    history = project.load("ready_history")
    assert len(history) == 1 and history[0]["final_review_id"] == "FR-SYNTH"
    ready_hash = history[0]["hashes"]["投标文件"]["pdf"]
    assert build(project, mode="ready")["status"] == "ready_for_signature"
    assert len(project.load("ready_history")) == 1, "同一次ready重复执行不重复堆叠快照"

    reviewed = build(project, render=False)
    assert reviewed["run_id"] != "RUNY"
    history = project.load("ready_history")
    assert len(history) == 1 and history[0]["hashes"]["投标文件"]["pdf"] == ready_hash
    assert all(project.safe_path(row["pdf"]).is_file() for row in history[0]["outputs"]), "旧ready成品文件必须保留"
