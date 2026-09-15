"""从拆标任务到可核验待签章文件的完整合成闭环。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from bidflow.assembly import build, verify_output
from bidflow.ingest import ingest
from bidflow.matching import audit, score
from bidflow.project import Project
from bidflow.utils import write_text
from bidflow.workflow import accept, confirm, prepare


@pytest.mark.skipif(os.environ.get("BIDFLOW_WORD_TEST") != "1", reason="需要显式执行真实Microsoft Word闭环")
def test_complete_agent_driven_bid_workflow(tmp_path: Path):
    project = Project.create(tmp_path / "project", "完整闭环合成投标")
    tender = tmp_path / "合成招标文件.md"
    write_text(tender, "资格要求：投标人具有有效法人证照。商务评分：有效证照得3分。技术评分：工作方案满10分，须说明工作步骤和质量复核。")
    tender_result = ingest(project, tender, "招标文件")
    block = project.load("blocks")[0]
    analyze_task = prepare(project, "analyze")["created"][0]["id"]
    source = lambda quote: [{"file_id": tender_result["file_id"], "block_id": block["id"], "quote": quote, "page": block.get("page")}]
    accept(project, analyze_task, {
        "rules": [
            {"kind": "qualification", "title": "法人证照", "text": "投标人具有有效法人证照", "sources": source("投标人具有有效法人证照"), "criteria": {"proof_mode": "evidence"}},
            {"kind": "business", "title": "有效证照", "text": "有效证照得3分", "sources": source("有效证照得3分"), "max_score": 3, "criteria": {"proof_mode": "evidence", "scoring": {"method": "fixed", "points": 3}}},
            {"kind": "technical", "title": "工作方案", "text": "工作方案满10分，须说明工作步骤和质量复核", "sources": source("工作方案满10分，须说明工作步骤和质量复核"), "max_score": 10, "subrequirements": ["工作步骤", "质量复核"]},
        ],
        "covered_block_ids": [block["id"]],
        "facts": {"company_name": "合成测试公司", "deadline": "2026-12-31", "service_period": "60日", "price": "10000"},
        "settings": {"anonymous": False, "allow_indices": True, "expected_total_score": 13},
        "conflicts": [], "retire_rule_ids": [],
    }, "拆标Agent")
    confirm(project, "rules", "规则确认人")

    image_path = tmp_path / "合成证照.png"
    image = Image.new("RGB", (1200, 800), "white")
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((30, 30, 1170, 770), outline="black", width=5)
    drawing.text((100, 100), "SYNTHETIC LICENSE FOR SOFTWARE TEST", fill="black")
    image.save(image_path)
    proof_result = ingest(project, image_path, "公司证照")
    evidence_task = prepare(project, "evidence", proof_result["file_id"])["created"][0]["id"]
    accept(project, evidence_task, {
        "evidence": [{"id": "E001", "file_id": proof_result["file_id"], "title": "合成测试证照", "pages": [1], "facts": {"company_name": "合成测试公司", "document_type": "法人证照"}, "verification": "verified", "notes": "已查看合成原图", "visually_verified": True}],
        "responses": [
            {"id": "RESP_Q", "rule_id": "Q001", "evidence_ids": ["E001"], "section_ids": [], "status": "supported", "rationale": "证照原图显示测试主体", "rule_revision": 1},
            {"id": "RESP_S", "rule_id": "S001", "evidence_ids": ["E001"], "section_ids": [], "status": "supported", "rationale": "同一证照支撑商务固定分", "rule_revision": 1},
        ],
    }, "证据Agent")
    confirm(project, "selection", "材料选择确认人")
    assert score(project)["business_supported_score"] == 3

    plan_task = prepare(project, "plan")["created"][0]["id"]
    accept(project, plan_task, {"plans": [{"id": "SEC001", "title": "工作方案", "rule_ids": ["T001"], "content_points": ["分阶段工作步骤", "三级质量复核"], "fact_refs": ["service_period"], "method_refs": [], "visuals": ["工作步骤表"]}]}, "策划Agent")
    facts = project.load("facts", {})
    facts["formal_brief"] = {
        "questions": ["文种用途", "发文主体", "主要读者", "决策目标", "正文范围", "事实依据"],
        "document_type": "咨询服务投标技术方案", "purpose": "响应技术评分", "author": "合成测试公司",
        "audience": "合成评标委员会", "decision_goal": "覆盖T001全部子要求", "body_scope": "工作方案",
        "sources": ["合成招标文件", "确认项目事实"], "structure": ["工作步骤", "质量复核"], "length": "短篇测试", "tone": "正式直接",
    }
    project.save("facts", facts)
    confirm(project, "brief", "正文沟通确认人")
    confirm(project, "plan", "技术策划确认人")

    write_task = prepare(project, "write", "SEC001")["created"][0]["id"]
    accept(project, write_task, {"section_id": "SEC001", "writer_id": "writer", "markdown": "# 工作方案\n\n## 工作步骤\n\n项目按资料核对、分析、编制和复核四个阶段推进。\n\n## 质量复核\n\n成果执行编制、校核、审核三级复核，并保留测试记录。", "covered_subrequirements": {"T001": ["工作步骤", "质量复核"]}}, "writer")
    review_task = prepare(project, "review", "SEC001")["created"][0]["id"]
    accept(project, review_task, {"section_id": "SEC001", "writer_id": "writer", "reviewer_id": "reviewer", "findings": [], "scores": [{"rule_id": "T001", "score": 9, "reason": "合成测试正文覆盖两个子要求"}], "covered_subrequirements": {"T001": ["工作步骤", "质量复核"]}, "status": "pass"}, "reviewer")
    confirm(project, "draft", "正文定稿确认人")
    before = audit(project)
    assert not any(issue["category"] in {"coverage", "evidence", "section", "review", "score"} for issue in before["issues"])

    review_build = build(project, mode="review", split=True, render=True)
    assert review_build["rendered"] and review_build["verified"] and len(review_build["outputs"]) == 2
    assert verify_output(project)["status"] == "passed"
    confirm(project, "assembly", "组卷内容确认人")
    confirm(project, "visual", "视觉验收确认人")
    ready = build(project, mode="ready")
    assert ready["status"] == "ready_for_signature" and ready["signature_status"] == "pending"
    assert all(project.safe_path(output["docx"]).is_file() and project.safe_path(output["pdf"]).is_file() for output in ready["outputs"])
    assert all(output["docx"].startswith("07_最终输出/") for output in ready["outputs"])
