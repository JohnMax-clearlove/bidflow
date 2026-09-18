"""证据、计分、人员配置与终审核查测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bidflow.matching import audit, match, optimize_staff, render_reports, score
from bidflow.project import Project
from bidflow.utils import json_hash, sha256_file, write_text


def base_project(tmp_path: Path) -> Project:
    project = Project.create(tmp_path / "project", "证据链合成测试")
    tender = project.safe_path("01_输入文件/01_招标文件/要求.md")
    proof = project.safe_path("01_输入文件/07_人员业绩证明/合同.txt")
    proof2 = project.safe_path("01_输入文件/07_人员业绩证明/成果.txt")
    write_text(tender, "项目负责人须主持类似项目。企业业绩每项2分，最高4分。技术方案说明工作步骤和质量复核。")
    write_text(proof, "合成合同：张三担任项目负责人，项目编号P1。")
    write_text(proof2, "合成成果：李四作为项目成员参与，项目编号P2。")
    files = [
        {"id": "DOC_T", "path": tender.relative_to(project.root).as_posix(), "sha256": sha256_file(tender), "category": "招标文件", "page_count": 1},
        {"id": "DOC_E1", "path": proof.relative_to(project.root).as_posix(), "sha256": sha256_file(proof), "category": "人员业绩证明", "page_count": 1},
        {"id": "DOC_E2", "path": proof2.relative_to(project.root).as_posix(), "sha256": sha256_file(proof2), "category": "人员业绩证明", "page_count": 1},
    ]
    blocks = [
        {"id": "B_T", "file_id": "DOC_T", "text": tender.read_text(encoding="utf-8"), "page": 1, "quality": "ok"},
        {"id": "B_E1", "file_id": "DOC_E1", "text": proof.read_text(encoding="utf-8"), "page": 1, "quality": "ok"},
        {"id": "B_E2", "file_id": "DOC_E2", "text": proof2.read_text(encoding="utf-8"), "page": 1, "quality": "ok"},
    ]
    source = lambda quote: [{"file_id": "DOC_T", "block_id": "B_T", "quote": quote, "page": 1}]
    rules = [
        {"id": "Q001", "kind": "qualification", "title": "负责人主持业绩", "text": "项目负责人须主持类似项目", "sources": source("项目负责人须主持类似项目"), "status": "confirmed", "revision": 1, "criteria": {"personnel": {"required_role": "项目负责人"}}},
        {"id": "S001", "kind": "business", "title": "企业业绩", "text": "企业业绩每项2分", "sources": source("企业业绩每项2分"), "status": "confirmed", "revision": 1, "max_score": 4, "criteria": {"scoring": {"method": "count", "points_per_item": 2, "cap": 4, "distinct_by": "project_id"}}},
        {"id": "T001", "kind": "technical", "title": "技术方案", "text": "技术方案说明工作步骤和质量复核", "sources": source("技术方案说明工作步骤和质量复核"), "status": "confirmed", "revision": 1, "max_score": 10, "subrequirements": ["工作步骤", "质量复核"]},
    ]
    project.commit({"files": files, "blocks": blocks, "rules": rules, "facts": {"project_name": "证据链合成测试", "company_name": "测试公司", "deadline": "2026-12-31", "service_period": "60日", "price": "10000"}}, reason="建立合成测试基准")
    coverage = [
        {"id": f"ECOV{number:03d}", "task_id": "TEST", "block_id": block["id"], "block_hash": json_hash(block),
         "file_id": block["file_id"], "file_sha256": next(row["sha256"] for row in files if row["id"] == block["file_id"])}
        for number, block in enumerate(blocks, 1) if block["file_id"] != "DOC_T"
    ]
    project.save("evidence_coverage", coverage, reason="建立合成测试证据覆盖基准")
    return project


def evidence(project: Project, *, eid="E001", file_id="DOC_E1", verification="verified", role="项目负责人", staff="STAFF1", project_id="P1", visually_verified=False, valid_until=None):
    file = next(row for row in project.load("files") if row["id"] == file_id)
    facts = {"project_id": project_id, "person_roles": [{"staff_id": staff, "role": role, "project_id": project_id}]}
    if valid_until:
        facts["valid_until"] = valid_until
    return {"id": eid, "title": eid, "file_id": file_id, "file_sha256": file["sha256"], "pages": [1], "facts": facts, "verification": verification, "verified_by": "材料核验员" if verification == "verified" else "", "visually_verified": visually_verified}


def response(rid, rule_id, evidence_ids, **extra):
    return {"id": rid, "rule_id": rule_id, "evidence_ids": evidence_ids, "section_ids": [], "status": "supported", "rationale": "已按原文和证明页核验", "score": 999, "missing": [], "rule_revision": 1, "covered_subrequirements": [], **extra}


def test_ledger_candidate_and_participation_do_not_prove_leadership(tmp_path):
    project = base_project(tmp_path)
    project.save("history", [{"id": "P2", "name": "台账项目", "contract_no": "HT2", "contract_date": "2026-01-01", "roles": [{"staff_id": "STAFF2", "role": "项目成员"}], "facts": {"project_type": "咨询", "verification": "ledger_only"}, "source_rows": [{"row": 2}]}])
    rules = project.load("rules")
    rules[0]["criteria"]["candidate_filter"] = {"staff_id": "STAFF2", "from_date": "2025-01-01"}
    project.save("rules", rules)
    project.save("evidence", [evidence(project, role="项目成员", staff="STAFF2", project_id="P2")])
    project.save("responses", [response("R1", "Q001", ["E001"], staff_id="STAFF2", project_id="P2")])
    result = match(project)
    assert not next(row for row in result["rules"] if row["rule_id"] == "Q001")["supported"]
    assert result["candidates"][0]["status"] == "candidate"
    assert "参与经历不能替代主持业绩" in str(result["missing"])


def test_verified_role_supports_rule_and_expired_certificate_fails(tmp_path):
    project = base_project(tmp_path)
    item = evidence(project, valid_until="2026-01-01")
    project.save("evidence", [item])
    project.save("responses", [response("R1", "Q001", ["E001"], staff_id="STAFF1", project_id="P1")])
    rules = project.load("rules")
    rules[0]["criteria"]["expiration_required"] = True
    project.save("rules", rules)
    assert not match(project)["rules"][0]["supported"]
    item["facts"]["valid_until"] = "2027-01-01"
    project.save("evidence", [item])
    assert match(project)["rules"][0]["supported"]


def test_score_separates_supported_potential_and_ignores_self_reported_score(tmp_path):
    project = base_project(tmp_path)
    first = evidence(project)
    duplicate = evidence(project, eid="E002")
    candidate = evidence(project, eid="E003", file_id="DOC_E2", verification="candidate", staff="STAFF2", role="项目成员", project_id="P2")
    project.save("evidence", [first, duplicate, candidate])
    project.save("responses", [response("R1", "S001", ["E001", "E002"]), response("R2", "S001", ["E003"])])
    result = score(project)
    assert result["business_supported_score"] == 2
    assert result["business_potential_additional_score"] == 2
    rules = project.load("rules")
    rules[1]["criteria"]["scoring"] = {"method": "manual"}
    project.save("rules", rules)
    manual = score(project)
    assert manual["business_supported_score"] == 0
    assert "自报分值不直接计入" in str(manual["business"][0]["problems"])


def test_low_quality_page_requires_visual_confirmation(tmp_path):
    project = base_project(tmp_path)
    blocks = project.load("blocks")
    blocks[1]["quality"] = "review"
    project.save("blocks", blocks)
    coverage = project.load("evidence_coverage")
    for row in coverage:
        if row["block_id"] == "B_E1":
            row["block_hash"] = json_hash(blocks[1])
    project.save("evidence_coverage", coverage, reason="合成测试：页面质量变化后更新覆盖哈希")
    item = evidence(project)
    project.save("evidence", [item])
    project.save("responses", [response("R1", "Q001", ["E001"], staff_id="STAFF1", project_id="P1")])
    assert not match(project)["rules"][0]["supported"]
    item["visually_verified"] = True
    project.save("evidence", [item])
    assert match(project)["rules"][0]["supported"]


def test_staff_optimization_respects_position_and_reports_truncation(tmp_path):
    project = base_project(tmp_path)
    project.save("staff", [
        {"id": "STAFF1", "name": "张三", "available": True, "facts": {}, "source_rows": [{"row": 2}]},
        {"id": "STAFF2", "name": "李四", "available": None, "facts": {}, "source_rows": [{"row": 3}]},
    ])
    project.save("evidence", [evidence(project), evidence(project, eid="E002", file_id="DOC_E2", staff="STAFF2", role="技术负责人", project_id="P2")])
    project.save("responses", [
        response("R1", "S001", ["E001"], staff_id="STAFF1", position_id="leader"),
        response("R2", "S001", ["E002"], staff_id="STAFF2", position_id="technical"),
    ])
    config = {"max_combinations": 10, "positions": [
        {"id": "leader", "title": "项目负责人", "candidate_ids": ["STAFF1"], "scoring_rule_ids": ["S001"]},
        {"id": "technical", "title": "技术负责人", "candidate_ids": ["STAFF2"], "scoring_rule_ids": ["S001"]},
    ]}
    result = optimize_staff(project, config)
    assert result["status"] == "complete" and result["recommendations"]
    assert result["recommendations"][0]["availability_pending"] == ["STAFF2"]
    config["max_combinations"] = 1
    config["positions"][0]["candidate_ids"] = ["STAFF1", "STAFF2"]
    truncated = optimize_staff(project, config)
    assert truncated["truncated"] and "未宣称最优" in truncated["summary"]


def test_audit_requires_current_independent_review_and_all_subrequirements(tmp_path):
    project = base_project(tmp_path)
    section_path = project.safe_path("05_投标文件编制/SEC001.md")
    write_text(section_path, "# 技术方案\n工作步骤和质量复核均形成记录。\n")
    section = {"id": "SEC001", "title": "技术方案", "path": section_path.relative_to(project.root).as_posix(), "sha256": sha256_file(section_path), "rule_ids": ["T001"], "status": "confirmed", "writer_id": "writer"}
    project.save("sections", [section])
    project.save("responses", [{**response("R1", "T001", []), "section_ids": ["SEC001"], "covered_subrequirements": ["工作步骤", "质量复核"]}])
    first = audit(project)
    assert any("独立复核" in issue["message"] for issue in first["issues"])
    review = {"id": "REV001", "section_id": "SEC001", "writer_id": "writer", "reviewer_id": "reviewer", "section_sha256": section["sha256"], "rule_revisions": {"T001": 1}, "facts_fingerprint": project.fingerprint(["facts"]), "round": 1, "status": "pass", "scores": [{"rule_id": "T001", "score": 8, "reason": "已逐项审核"}], "covered_subrequirements": {"T001": ["工作步骤"]}, "findings": []}
    project.save("reviews", [review])
    second = audit(project)
    assert any("质量复核" in issue["message"] for issue in second["issues"] if issue["category"] == "review")
    review["covered_subrequirements"]["T001"].append("质量复核")
    project.save("reviews", [review])
    third = audit(project)
    assert not any(issue["category"] == "review" and "当前正文" in issue["message"] for issue in third["issues"])


def test_changed_source_hash_invalidates_evidence(tmp_path):
    project = base_project(tmp_path)
    item = evidence(project)
    project.save("evidence", [item])
    project.save("responses", [response("R1", "Q001", ["E001"], staff_id="STAFF1", project_id="P1")])
    path = project.safe_path(next(f["path"] for f in project.load("files") if f["id"] == "DOC_E1"))
    write_text(path, "人工更改了原始合同")
    result = match(project)
    assert not result["rules"][0]["supported"]
    assert "原始文件已变化" in str(result["missing"])


def test_reports_include_bid_spec_and_technical_matrix(tmp_path):
    project = base_project(tmp_path)
    project.save("fact_proposals", {"deadline": "2026-12-31"})
    project.save("setting_proposals", {"expected_total_score": 100})
    result = render_reports(project)
    relative = {Path(path).relative_to(project.root).as_posix() for path in result["paths"]}
    assert "02_招标拆解/bid_spec.json" in relative
    assert "04_技术标策划/技术评分响应矩阵.md" in relative
    assert "04_技术标策划/技术标策划.md" in relative
    assert project.safe_path("03_资料匹配/待补资料及借阅清单.md").is_file()
    spec = __import__("json").loads(project.safe_path("02_招标拆解/bid_spec.json").read_text(encoding="utf-8"))
    assert spec["proposed_facts"]["deadline"] == "2026-12-31"
    assert spec["proposed_settings"]["expected_total_score"] == 100
