"""成品双审合成测试辅助；全部为虚构数据，不包含真实公司或项目资料。"""

from __future__ import annotations

from pathlib import Path

from bidflow import final_review
from bidflow.ingest import ingest
from bidflow.project import Project
from bidflow.utils import write_text
from bidflow.workflow import accept, confirm, prepare as prepare_task


def make_pdf(path: Path, pages: int = 2, marker: str = "SYNTHETIC FINAL REVIEW", blank: bool = False) -> Path:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    for number in range(1, pages + 1):
        page = writer.add_blank_page(width=595, height=842)
        if blank:
            continue
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        content = b"BT /F1 12 Tf 60 700 Td (" + f"{marker} page {number}".encode("ascii", "replace") + b") Tj ET"
        stream = DecodedStreamObject()
        stream.set_data(content)
        page[NameObject("/Resources")] = resources
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(str(path))
    return path


def make_project(tmp_path: Path, name: str = "成品双审合成项目") -> Project:
    """合成项目：完整分块、六类规则均已确认，尚无可组卷正文。"""
    project = Project.create(tmp_path / name, name)
    source = tmp_path / f"{name}_招标.md"
    write_text(source, "资格要求：投标人具有有效法人证照。否决情形：报价超过最高限价。必响应：承诺服务期满足要求。"
                      "商务评分：有效证照得3分。技术评分：工作方案须说明工作步骤和质量复核。报价评分：按评标价计算。")
    imported = ingest(project, source, "招标文件")
    block = project.load("blocks")[0]
    task = prepare_task(project, "analyze")["created"][0]["id"]

    def source_ref(quote: str) -> dict:
        return {"file_id": imported["file_id"], "block_id": block["id"], "quote": quote, "page": block.get("page")}

    accept(project, task, {
        "rules": [
            {"kind": "qualification", "title": "法人证照", "text": "投标人具有有效法人证照", "sources": [source_ref("投标人具有有效法人证照")], "criteria": {"proof_mode": "evidence"}},
            {"kind": "invalid", "title": "超最高限价", "text": "报价超过最高限价", "sources": [source_ref("报价超过最高限价")]},
            {"kind": "requirement", "title": "服务期承诺", "text": "承诺服务期满足要求", "sources": [source_ref("承诺服务期满足要求")], "criteria": {"proof_mode": "either"}},
            {"kind": "business", "title": "有效证照得分", "text": "有效证照得3分", "sources": [source_ref("有效证照得3分")], "max_score": 3, "criteria": {"proof_mode": "evidence", "scoring": {"method": "fixed", "points": 3}}},
            {"kind": "technical", "title": "工作方案", "text": "工作方案须说明工作步骤和质量复核", "sources": [source_ref("工作方案须说明工作步骤和质量复核")], "max_score": 10, "subrequirements": ["工作步骤", "质量复核"]},
            {"kind": "price", "title": "报价评分", "text": "按评标价计算", "sources": [source_ref("按评标价计算")], "max_score": 20, "criteria": {"scoring": {"method": "manual"}}},
        ],
        "covered_block_ids": [block["id"]],
        "facts": {"company_name": "合成测试公司", "deadline": "2026-12-31", "service_period": "60日", "price": "10000"},
        "settings": {"anonymous": False, "allow_indices": True, "expected_total_score": 33},
        "conflicts": [], "retire_rule_ids": [],
    }, "规则分析Agent")
    confirm(project, "rules", "规则确认人", attest_human=True)
    return project


def pass_items(project: Project, review_id: str, lane: str, actor: str, overrides: dict | None = None) -> dict:
    """按当前check_items生成一份逐项全通过结果；overrides按核查项ID覆盖字段。"""
    review = final_review._get(project, review_id)
    overrides = overrides or {}
    parent = final_review._get(project, review["parent"]) if review.get("parent") else None
    items = []
    for check in review["check_items"]:
        override = overrides.get(check["id"], {})
        status = override.get("status", "pass")
        refs = override.get("refs")
        if refs is None:
            refs = [{"volume": review["inputs"][0]["id"], "page": 1}] if status != "not_applicable" else []
        row = {"id": check["id"], "status": status,
               "reason": override.get("reason", f"{check['title']}：合成测试逐项核查，结论{status}。"),
               "refs": refs, "parent_refs": override.get("parent_refs", []),
               "verified_original": override.get("verified_original", True), "blocking": override.get("blocking", False)}
        if parent and check["kind"] == "rule":
            row["parent_refs"] = override.get("parent_refs", [{"volume": parent["inputs"][0]["id"], "page": 1}])
        if override.get("score"):
            row["score"] = override["score"]
        items.append(row)
    return {"review_id": review_id, "lane": lane, "actor": actor, "items": items, "summary": "合成测试双审结果"}


def submit_pass(project: Project, review_id: str, lane: str, actor: str, overrides: dict | None = None) -> dict:
    result = pass_items(project, review_id, lane, actor, overrides)
    return final_review.submit(project, review_id, lane, result, actor, attest_human=(lane == "human"))


def complete_content(project: Project, writer: str = "writer-A", agent: str = "双审Agent", human: str = "人工复核人") -> str:
    """对当前组卷成品完成内容双审；返回通过的review_id。"""
    review = final_review.start(project, use_assembly=True, stage="content", writer=writer)
    review_id = review["id"]
    for lane, actor in (("agent", agent), ("human", human)):
        final_review.prepare(project, review_id, lane)
        submit_pass(project, review_id, lane, actor)
    state = final_review.finalize(project, review_id)
    if state["status"] != "content_review_passed":
        raise AssertionError(f"内容双审未通过：{state['status']} {state['reasons']}")
    return review_id


def complete_signed(project: Project, parent: str, pdfs: list[Path], writer: str = "writer-A",
                    agent: str = "签章核查Agent", human: str = "签章人工复核人") -> str:
    review = final_review.start(project, pdfs=pdfs, stage="signed", writer=writer, parent=parent)
    review_id = review["id"]
    for lane, actor in (("agent", agent), ("human", human)):
        final_review.prepare(project, review_id, lane)
        submit_pass(project, review_id, lane, actor)
    state = final_review.finalize(project, review_id)
    if state["status"] != "signed_review_passed":
        raise AssertionError(f"签章双审未通过：{state['status']} {state['reasons']}")
    return review_id


def confirm_submission(project: Project, review_id: str, actor: str = "递交确认人", statuses: dict | None = None) -> dict:
    statuses = statuses or {}
    result = {"review_id": review_id, "actor": actor,
              "items": [{"id": item["id"], "status": statuses.get(item["id"], "confirmed"), "reason": "合成测试：已对照招标要求逐项核对"}
                        for item in final_review._submission_items()]}
    return final_review.submission(project, review_id, result, actor, attest_human=True)
