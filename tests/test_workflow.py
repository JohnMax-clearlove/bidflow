"""可恢复任务、来源校验、人工确认和改稿保护测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bidflow.ingest import ingest
from bidflow.matching import match
from bidflow.project import Project
from bidflow.utils import json_hash, sha256_file, write_text
from bidflow.workflow import accept, confirm, coverage, is_confirmed, next_steps, prepare, sync


def make_project(tmp_path: Path) -> Project:
    project = Project.create(tmp_path / "项目", "工作流合成测试")
    source = tmp_path / "招标文件.md"
    write_text(source, "资格要求：投标人具有法人资格。技术评分：工作方案应说明工作步骤和质量复核。")
    imported = ingest(project, source, "招标文件")
    block = project.load("blocks")[0]
    package = prepare(project, "analyze")
    task = package["created"][0]["id"]
    result = {
        "rules": [
            {"kind": "qualification", "title": "法人资格", "text": "投标人具有法人资格", "sources": [{"file_id": imported["file_id"], "block_id": block["id"], "quote": "投标人具有法人资格", "page": block.get("page")}], "criteria": {"proof_mode": "evidence"}},
            {"kind": "technical", "title": "工作方案", "text": "工作方案应说明工作步骤和质量复核", "sources": [{"file_id": imported["file_id"], "block_id": block["id"], "quote": "工作方案应说明工作步骤和质量复核", "page": block.get("page")}], "max_score": 10, "subrequirements": ["工作步骤", "质量复核"]},
        ],
        "covered_block_ids": [block["id"]],
        "facts": {"company_name": "合成测试公司", "deadline": "2026-12-31", "service_period": "60日", "price": "10000"},
        "settings": {"allow_indices": True, "anonymous": False, "expected_total_score": 10},
        "conflicts": [],
        "retire_rule_ids": [],
    }
    accept(project, task, result, "规则分析Agent")
    confirm(project, "rules", "规则确认人")
    return project


def formal_brief():
    return {
        "questions": ["文种用途", "发文主体", "主要读者", "决策目标", "正文范围", "依据来源", "结构主线", "篇幅语气"],
        "document_type": "咨询服务投标技术方案",
        "purpose": "响应招标技术评分",
        "author": "合成测试投标人",
        "audience": "评标委员会",
        "decision_goal": "完整响应技术评分项",
        "body_scope": "技术方案正文",
        "sources": ["招标文件", "已确认项目事实"],
        "structure": ["工作步骤", "质量复核"],
        "length": "合成测试短文",
        "tone": "正式、直接、精炼",
    }


def prepare_plan_and_brief(project: Project):
    task = prepare(project, "plan")["created"][0]["id"]
    accept(project, task, {"plans": [{"id": "SEC001", "title": "工作方案", "rule_ids": ["T001"], "content_points": ["工作步骤", "质量复核"], "fact_refs": ["project_name"], "method_refs": [], "visuals": ["流程表"]}]}, "技术策划Agent")
    facts = project.load("facts", {})
    facts["formal_brief"] = formal_brief()
    project.save("facts", facts, reason="记录正式文本沟通确认内容")
    confirm(project, "brief", "正文确认人")
    confirm(project, "plan", "策划确认人")


def test_full_analysis_coverage_and_source_validation(tmp_path):
    project = make_project(tmp_path)
    assert coverage(project)["complete"]
    assert is_confirmed(project, "rules")
    assert [row["id"] for row in project.load("rules")] == ["Q001", "T001"]
    assert all(row["status"] == "confirmed" for row in project.load("rules"))
    package = next(project.safe_path(row["package_path"]) for row in project.load("tasks") if row["stage"] == "analyze")
    package_data = __import__("json").loads(package.read_text(encoding="utf-8"))
    assert any("资格Q" in item for item in package_data["instructions"])
    assert "不是操作指令" in package_data["data_notice"]
    bad_project = Project.create(tmp_path / "bad", "坏来源测试")
    source = tmp_path / "坏来源.md"
    write_text(source, "资格要求")
    ingest(bad_project, source)
    task = prepare(bad_project, "analyze")["created"][0]["id"]
    block = bad_project.load("blocks")[0]
    with pytest.raises(ValueError, match="引文"):
        accept(bad_project, task, {"rules": [{"kind": "qualification", "title": "错误", "text": "错误", "sources": [{"file_id": block["file_id"], "block_id": block["id"], "quote": "原文里没有", "page": None}]}], "covered_block_ids": [block["id"]]}, "错误Agent")


def test_amendment_preserves_prior_rule_and_invalidates_old_response(tmp_path):
    project = make_project(tmp_path)
    q = next(row for row in project.load("rules") if row["id"] == "Q001")
    project.save("responses", [{"id": "RESP1", "rule_id": "Q001", "evidence_ids": [], "section_ids": [], "status": "supported", "rationale": "旧版响应", "missing": [], "rule_revision": q["revision"], "covered_subrequirements": []}])
    amendment = tmp_path / "补遗.md"
    write_text(amendment, "补遗：法人资格还需提供有效营业执照。")
    imported = ingest(project, amendment, "澄清补遗")
    task = prepare(project, "amendment")["created"][0]["id"]
    block = next(row for row in project.load("blocks") if row["file_id"] == imported["file_id"])
    accept(project, task, {"rules": [{"id": "Q001", "kind": "qualification", "title": "法人资格", "text": "法人资格还需提供有效营业执照", "sources": [{"file_id": imported["file_id"], "block_id": block["id"], "quote": "法人资格还需提供有效营业执照", "page": block.get("page")}], "criteria": {"proof_mode": "evidence"}}], "covered_block_ids": [block["id"]], "retire_rule_ids": [], "conflicts": []}, "补遗分析Agent")
    revised = next(row for row in project.load("rules") if row["id"] == "Q001")
    assert revised["revision"] == 2 and revised["previous_versions"][0]["revision"] == 1
    assert not is_confirmed(project, "rules")
    confirm(project, "rules", "补遗确认人")
    assert "旧版规则" in str(match(project)["missing"])


def test_formal_brief_gate_and_manual_edit_conflict(tmp_path):
    project = make_project(tmp_path)
    task = prepare(project, "plan")["created"][0]["id"]
    accept(project, task, {"plans": [{"id": "SEC001", "title": "工作方案", "rule_ids": ["T001"], "content_points": ["工作步骤", "质量复核"]}]}, "策划Agent")
    confirm(project, "plan", "策划确认人")
    with pytest.raises(ValueError, match="正式文本沟通"):
        prepare(project, "write")
    facts = project.load("facts", {})
    facts["formal_brief"] = formal_brief()
    project.save("facts", facts)
    confirm(project, "brief", "正文确认人")
    confirm(project, "plan", "策划再次确认人")
    write_task = prepare(project, "write", "SEC001")["created"][0]["id"]
    path = project.safe_path("05_投标文件编制/SEC001_工作方案.md")
    write_text(path, "# 用户手工稿\n保留这段人工修改。\n")
    result = accept(project, write_task, {"section_id": "SEC001", "markdown": "# Agent稿\n候选内容。", "writer_id": "writer", "covered_subrequirements": {"T001": ["工作步骤", "质量复核"]}}, "writer")
    assert result["status"] == "冲突，原稿未覆盖"
    assert "用户手工稿" in path.read_text(encoding="utf-8")
    assert project.safe_path(result["candidate"]).is_file()


def test_write_review_independence_versioning_and_resume(tmp_path):
    project = make_project(tmp_path)
    prepare_plan_and_brief(project)
    write_task = prepare(project, "write", "SEC001")["created"][0]["id"]
    accept(project, write_task, {"section_id": "SEC001", "markdown": "# 工作方案\n工作步骤明确，质量复核形成记录。", "writer_id": "writer", "covered_subrequirements": {"T001": ["工作步骤", "质量复核"]}}, "writer")
    review_task = prepare(project, "review", "SEC001")["created"][0]["id"]
    same_person = {"section_id": "SEC001", "reviewer_id": "writer", "writer_id": "writer", "findings": [], "scores": [{"rule_id": "T001", "score": 8, "reason": "模拟"}], "covered_subrequirements": {"T001": ["工作步骤", "质量复核"]}, "status": "pass"}
    with pytest.raises(ValueError, match="不能相同"):
        accept(project, review_task, same_person, "writer")
    good = {**same_person, "reviewer_id": "reviewer"}
    accept(project, review_task, good, "reviewer")
    review = project.load("reviews")[0]
    assert review["rule_revisions"] == {"T001": 1}
    assert review["facts_fingerprint"] == project.fingerprint(["facts"])
    reopened = Project(project.root)
    assert reopened.load("tasks") == project.load("tasks")
    assert not next_steps(reopened)["pending_tasks"]


def test_input_change_marks_task_stale(tmp_path):
    project = make_project(tmp_path)
    prepare_plan_and_brief(project)
    task = prepare(project, "write", "SEC001")["created"][0]["id"]
    facts = project.load("facts", {})
    facts["service_period"] = "90日"
    project.save("facts", facts)
    stale = sync(project)
    assert task in stale["stale"]
    assert next(row for row in project.load("tasks") if row["id"] == task)["status"] == "stale"


def test_technical_response_does_not_invalidate_material_selection(tmp_path):
    project = make_project(tmp_path)
    proof = project.safe_path("01_输入文件/02_公司证照/营业执照.txt")
    write_text(proof, "合成测试营业执照")
    files = project.load("files")
    files.append({"id": "DOC_PROOF", "path": proof.relative_to(project.root).as_posix(), "sha256": sha256_file(proof), "category": "公司证照", "page_count": 1})
    evidence = [{"id": "E001", "title": "营业执照", "file_id": "DOC_PROOF", "file_sha256": sha256_file(proof), "pages": [1], "facts": {"company_name": "合成测试公司"}, "verification": "verified", "verified_by": "材料核验人"}]
    responses = [{"id": "RESP_Q", "rule_id": "Q001", "evidence_ids": ["E001"], "section_ids": [], "status": "supported", "rationale": "证明法人资格", "missing": [], "rule_revision": 1, "covered_subrequirements": []}]
    project.commit({"files": files, "evidence": evidence, "responses": responses})
    confirm(project, "selection", "材料选择确认人")
    assert is_confirmed(project, "selection")
    responses.append({"id": "RESP_T", "rule_id": "T001", "evidence_ids": [], "section_ids": ["SEC001"], "status": "supported", "rationale": "正文响应", "missing": [], "rule_revision": 1, "covered_subrequirements": ["工作步骤", "质量复核"]})
    project.save("responses", responses)
    assert is_confirmed(project, "selection")


def test_project_paths_and_optimistic_commit(tmp_path):
    project = Project.create(tmp_path / "project", "项目保护测试")
    with pytest.raises(ValueError, match="超出"):
        project.safe_path("../逃逸.txt")
    digest = project.fingerprint(["facts"])
    project.save("facts", {"project_name": "另一版本"})
    with pytest.raises(RuntimeError, match="更新"):
        project.commit({"facts": {"project_name": "旧写入"}}, expected={"facts": digest})
    with pytest.raises(ValueError, match="不是空目录"):
        Project.create(project.root, "覆盖项目")
