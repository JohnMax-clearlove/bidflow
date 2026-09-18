"""成品双审合成测试：外部PDF、两lane、风险与分数分离、版本失效和提交前检查。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from bidflow import final_review
from bidflow.utils import sha256_file, write_text
from final_review_helpers import (complete_content, complete_signed, confirm_submission, make_pdf, make_project,
                                  pass_items, submit_pass)


def test_external_pdf_dual_review_without_sections_or_assembly(tmp_path):
    """外部人工组卷PDF无需sections/plans/assembly，也能完成独立双审。"""
    project = make_project(tmp_path)
    assert project.load("sections") == [] and not project.load("assembly")
    pdf = make_pdf(tmp_path / "人工组卷.pdf", pages=2, marker="EXTERNAL PDF")
    original_hash = sha256_file(pdf)
    review = final_review.start(project, pdfs=[pdf], stage="content", writer="writer-A")
    review_id = review["id"]
    volume = review["inputs"][0]
    assert volume["page_count"] == 2 and volume["sha256"] == original_hash
    assert project.safe_path(volume["path"]).is_file()
    assert sha256_file(pdf) == original_hash, "启动双审不得改动外部原件"
    assert review["check_item_count"] >= 6

    human = final_review.prepare(project, review_id, "human")
    assert project.safe_path(human["checklist"]).is_file()
    template = json.loads(project.safe_path(human["template"]).read_text(encoding="utf-8"))
    checks = final_review._get(project, review_id)["check_items"]
    assert {item["id"] for item in template["items"]} == {item["id"] for item in checks}
    assert {"CHK-Q001", "CHK-F001", "CHK-R001", "CHK-S001", "CHK-T001", "CHK-P001"} <= {item["id"] for item in checks}
    agent = final_review.prepare(project, review_id, "agent")
    assert project.safe_path(agent["batches"][0]["context"]).is_file()

    submit_pass(project, review_id, "agent", "核查Agent")
    state = final_review.finalize(project, review_id)
    assert state["status"] == "awaiting_lanes" and state["missing"]["human"]
    submit_pass(project, review_id, "human", "人工复核人")
    state = final_review.finalize(project, review_id)
    assert state["status"] == "content_review_passed"
    assert "签章" in state["next"]
    assert project.safe_path(state["report"]["markdown"]).is_file()
    refreshed = project.refresh_status()
    assert any(row["id"] == review_id for row in refreshed["final_reviews"])
    assert "成品双审" in project.safe_path("项目状态.md").read_text(encoding="utf-8")
    from bidflow.workflow import next_steps
    assert any("成品双审" in action for action in next_steps(project)["next"])
    assert not final_review.assembly_gate(project)["ok"], "没有组卷成品时门禁不得通过"


def test_corrupt_and_encrypted_pdf_are_rejected(tmp_path):
    project = make_project(tmp_path)
    broken = tmp_path / "损坏.pdf"
    broken.write_bytes(b"not a pdf at all")
    with pytest.raises(ValueError, match="无法打开"):
        final_review.start(project, pdfs=[broken], writer="writer-A")
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.encrypt("secret")
    locked = tmp_path / "加密.pdf"
    writer.write(str(locked))
    with pytest.raises(ValueError, match="加密"):
        final_review.start(project, pdfs=[locked], writer="writer-A")
    assert project.load("final_reviews") == [], "被拒绝的PDF不得产生双审记录"


def test_multiple_pdfs_and_failed_start_leaves_no_locked_record(tmp_path):
    project = make_project(tmp_path)
    good = make_pdf(tmp_path / "第一册.pdf", pages=1, marker="VOLUME ONE")
    broken = tmp_path / "第二册.pdf"
    broken.write_bytes(b"broken pdf")
    with pytest.raises(ValueError, match="无法打开"):
        final_review.start(project, pdfs=[good, broken], writer="writer-A")
    assert project.load("final_reviews") == [], "锁定失败不得留下半成品记录"
    fixed = make_pdf(tmp_path / "第二册.pdf", pages=2, marker="VOLUME TWO")
    review = final_review.start(project, pdfs=[good, fixed], writer="writer-A")
    assert len(review["inputs"]) == 2
    assert [volume["page_count"] for volume in review["inputs"]] == [1, 2]
    assert all(project.safe_path(volume["path"]).is_file() for volume in review["inputs"])


def test_partial_lane_submission_keeps_gap_until_covered(tmp_path):
    project = make_project(tmp_path)
    pdf = make_pdf(tmp_path / "partial.pdf", pages=1)
    review_id = final_review.start(project, pdfs=[pdf], writer="writer-A")["id"]
    final_review.prepare(project, review_id, "agent")
    final_review.prepare(project, review_id, "human")
    submit_pass(project, review_id, "agent", "核查Agent")
    human_result = pass_items(project, review_id, "human", "人工复核人")
    dropped = human_result["items"].pop(0)["id"]
    final_review.submit(project, review_id, "human", human_result, "人工复核人", attest_human=True)
    state = final_review.finalize(project, review_id)
    assert state["status"] == "awaiting_lanes" and dropped in state["missing"]["human"]


def test_human_attestation_and_lane_actor_separation(tmp_path):
    project = make_project(tmp_path)
    pdf = make_pdf(tmp_path / "actors.pdf", pages=1)
    review_id = final_review.start(project, pdfs=[pdf], writer="writer-A")["id"]
    for lane in ("agent", "human"):
        final_review.prepare(project, review_id, lane)

    with pytest.raises(ValueError, match="attest-human"):
        final_review.submit(project, review_id, "human", pass_items(project, review_id, "human", "人工A"), "人工A")
    with pytest.raises(ValueError, match="actor与命令actor不一致"):
        final_review.submit(project, review_id, "human", pass_items(project, review_id, "human", "人工A"), "人工B", attest_human=True)
    with pytest.raises(ValueError, match="不得使用 --attest-human"):
        final_review.submit(project, review_id, "agent", pass_items(project, review_id, "agent", "核查Agent"), "核查Agent", attest_human=True)

    # 用户本人既编制又人工核查：human lane允许等于writer；Agent lane不得与writer或human同一actor。
    submit_pass(project, review_id, "human", "writer-A")
    with pytest.raises(ValueError, match="不得等于编制者writer"):
        final_review.submit(project, review_id, "agent", pass_items(project, review_id, "agent", "writer-A"), "writer-A")

    submit_pass(project, review_id, "human", "同一个人")
    with pytest.raises(ValueError, match="不同actor"):
        final_review.submit(project, review_id, "agent", pass_items(project, review_id, "agent", "同一个人"), "同一个人")
    submit_pass(project, review_id, "agent", "核查Agent")
    assert final_review.finalize(project, review_id)["status"] == "content_review_passed"


def test_stale_snapshot_commit_is_rejected_and_history_preserved(tmp_path):
    project = make_project(tmp_path)
    pdf = make_pdf(tmp_path / "optimistic.pdf", pages=1)
    review_id = final_review.start(project, pdfs=[pdf], writer="writer-A")["id"]
    for lane in ("agent", "human"):
        final_review.prepare(project, review_id, lane)
    snapshot = final_review._get(project, review_id)
    stale = project.fingerprint(["final_reviews"])
    snapshot["revisions"] = [*snapshot.get("revisions", []), {"revision": 1, "lane": "agent", "actor": "核查Agent", "items": [], "result_hash": "a"}]
    final_review._save(project, snapshot, "模拟Agent先提交", expected=stale)
    other = {**final_review._get(project, review_id), "id": review_id}
    other["revisions"] = [*other.get("revisions", []), {"revision": 1, "lane": "human", "actor": "人工复核人", "items": [], "result_hash": "b"}]
    with pytest.raises(RuntimeError, match="更新"):
        final_review._save(project, other, "模拟人工用旧快照提交", expected=stale)
    revisions = final_review._get(project, review_id)["revisions"]
    assert [row["lane"] for row in revisions] == ["agent"], "旧快照提交不得丢掉已写入的lane历史"


def test_q_disagreement_requires_closure_before_pass(tmp_path):
    project = make_project(tmp_path)
    pdf = make_pdf(tmp_path / "closure.pdf", pages=1)
    review_id = final_review.start(project, pdfs=[pdf], writer="writer-A")["id"]
    for lane in ("agent", "human"):
        final_review.prepare(project, review_id, lane)
    submit_pass(project, review_id, "agent", "核查Agent")
    submit_pass(project, review_id, "human", "人工复核人", {"CHK-Q001": {"status": "not_applicable", "reason": "人工认为本项目不适用，需与Agent解释或闭环"}})
    state = final_review.finalize(project, review_id)
    assert state["status"] == "blocked"
    assert any(row["item_id"] == "CHK-Q001" and row["requires_closure"] for row in state["closure_required"])
    assert any("结论不一致" in reason for reason in state["reasons"])
    # 同版本解释闭环后才可通过。
    submit_pass(project, review_id, "human", "人工复核人")
    assert final_review.finalize(project, review_id)["status"] == "content_review_passed"


def test_inaccessible_external_original_keeps_locked_copy_reviewable(tmp_path):
    project = make_project(tmp_path)
    pdf = make_pdf(tmp_path / "原件将移走.pdf", pages=1)
    review_id = final_review.start(project, pdfs=[pdf], writer="writer-A")["id"]
    for lane in ("agent", "human"):
        final_review.prepare(project, review_id, lane)
    moved = tmp_path / "已移走" / pdf.name
    moved.parent.mkdir(parents=True, exist_ok=True)
    pdf.replace(moved)
    state = final_review.status(project, review_id)
    assert state["fresh"] is True
    assert any("锁定副本仍作为本次真实审阅对象" in warning for warning in state["warnings"])
    submit_pass(project, review_id, "agent", "核查Agent")
    submit_pass(project, review_id, "human", "人工复核人")
    final = final_review.finalize(project, review_id)
    assert final["status"] == "content_review_passed"
    assert any("无法核实递交原件" in warning for warning in final["warnings"])
    report = project.safe_path(final["report"]["markdown"]).read_text(encoding="utf-8")
    assert "无法核实递交原件" in report, "报告必须标清锁定副本与递交原件的界限"


def test_unknown_duplicate_out_of_range_and_empty_reason_are_rejected(tmp_path):
    project = make_project(tmp_path)
    pdf = make_pdf(tmp_path / "protocol.pdf", pages=1)
    review = final_review.start(project, pdfs=[pdf], writer="writer-A")
    review_id = review["id"]
    final_review.prepare(project, review_id, "agent")

    unknown = pass_items(project, review_id, "agent", "核查Agent")
    unknown["items"][0]["id"] = "CHK-NOPE"
    with pytest.raises(ValueError, match="未知"):
        final_review.submit(project, review_id, "agent", unknown, "核查Agent")

    duplicate = pass_items(project, review_id, "agent", "核查Agent")
    duplicate["items"].append(dict(duplicate["items"][0]))
    with pytest.raises(ValueError, match="重复"):
        final_review.submit(project, review_id, "agent", duplicate, "核查Agent")

    out_of_range = pass_items(project, review_id, "agent", "核查Agent", {"CHK-Q001": {"refs": [{"volume": "V001", "page": 99}]}})
    with pytest.raises(ValueError, match="超出"):
        final_review.submit(project, review_id, "agent", out_of_range, "核查Agent")

    empty_reason = pass_items(project, review_id, "agent", "核查Agent", {"CHK-Q001": {"reason": "   "}})
    with pytest.raises(ValueError, match="不符合协议"):
        final_review.submit(project, review_id, "agent", empty_reason, "核查Agent")

    bad_status = pass_items(project, review_id, "agent", "核查Agent", {"CHK-Q001": {"status": "maybe"}})
    with pytest.raises(ValueError, match="不符合协议"):
        final_review.submit(project, review_id, "agent", bad_status, "核查Agent")

    reference_from_rule = pass_items(project, review_id, "agent", "核查Agent", {"CHK-Q001": {"refs": [{"volume": "V001", "page": 0}]}})
    with pytest.raises(ValueError, match="不符合协议"):
        final_review.submit(project, review_id, "agent", reference_from_rule, "核查Agent")


def test_scanned_page_requires_original_verification(tmp_path):
    project = make_project(tmp_path)
    blank = make_pdf(tmp_path / "扫描件.pdf", pages=1, blank=True)
    review_id = final_review.start(project, pdfs=[blank], writer="writer-A")["id"]
    final_review.prepare(project, review_id, "agent")
    unchecked = pass_items(project, review_id, "agent", "核查Agent", {"CHK-Q001": {"verified_original": False}})
    with pytest.raises(ValueError, match="原页"):
        final_review.submit(project, review_id, "agent", unchecked, "核查Agent")


def test_qualification_risk_blocks_but_score_loss_does_not(tmp_path):
    project = make_project(tmp_path)
    pdf = make_pdf(tmp_path / "risk.pdf", pages=1)
    review_id = final_review.start(project, pdfs=[pdf], writer="writer-A")["id"]
    for lane in ("agent", "human"):
        final_review.prepare(project, review_id, lane)

    submit_pass(project, review_id, "agent", "核查Agent", {
        "CHK-Q001": {"status": "risk", "reason": "证照性质不明，须人工再核"},
        "CHK-S001": {"status": "risk", "reason": "缺少业绩材料，本评分项可能为0分", "blocking": False},
    })
    submit_pass(project, review_id, "human", "人工复核人", {"CHK-S001": {"status": "risk", "reason": "本评分项缺件，失分但不否决"}})
    state = final_review.finalize(project, review_id)
    assert state["status"] == "blocked"
    assert any(row["item_id"] == "CHK-Q001" for row in state["blocking"])
    assert not any(row["item_id"] == "CHK-S001" for row in state["blocking"]), "单纯评分失分不得升级废标"

    # 证书真实性等显式blocking发现即使落在评分项也阻断。
    submit_pass(project, review_id, "agent", "核查Agent", {
        "CHK-S001": {"status": "risk", "reason": "证照真实性无法确认", "blocking": True},
    })
    state = final_review.finalize(project, review_id)
    assert any(row["item_id"] == "CHK-S001" for row in state["blocking"])


def test_content_signature_todo_does_not_fail_content(tmp_path):
    """“签章前尚无签字”是阶段性待办，不得误判content失败。"""
    project = make_project(tmp_path)
    pdf = make_pdf(tmp_path / "todo.pdf", pages=1)
    review_id = final_review.start(project, pdfs=[pdf], writer="writer-A")["id"]
    for lane in ("agent", "human"):
        final_review.prepare(project, review_id, lane)
    override = {"CHK-STAGE-CONTENT-NO-SIGN": {"status": "risk", "reason": "提前盖章的风险记入签章阶段处理，不作为内容否决"}}
    submit_pass(project, review_id, "agent", "核查Agent", override)
    submit_pass(project, review_id, "human", "人工复核人", override)
    state = final_review.finalize(project, review_id)
    assert state["status"] == "content_review_passed"
    assert not any(row["item_id"] == "CHK-STAGE-CONTENT-NO-SIGN" for row in state["blocking"])


def test_score_kinds_ranges_and_tiers_are_checked(tmp_path):
    project = make_project(tmp_path)
    rules = project.load("rules")
    for rule in rules:
        if rule["id"] == "S001":
            rule["criteria"]["scoring"] = {"method": "tiered", "tiers": [{"min_count": 1, "points": 1}, {"min_count": 2, "points": 3}], "cap": 3}
    project.save("rules", rules, reason="合成测试：改为分档评分")
    from bidflow.workflow import confirm
    confirm(project, "rules", "规则确认人", attest_human=True)
    pdf = make_pdf(tmp_path / "score.pdf", pages=1)
    review_id = final_review.start(project, pdfs=[pdf], writer="writer-A")["id"]
    final_review.prepare(project, review_id, "agent")

    wrong_kind = pass_items(project, review_id, "agent", "核查Agent", {"CHK-T001": {"score": {"kind": "supported", "value": 8}}})
    with pytest.raises(ValueError, match="模拟分"):
        final_review.submit(project, review_id, "agent", wrong_kind, "核查Agent")
    over_max = pass_items(project, review_id, "agent", "核查Agent", {"CHK-S001": {"score": {"kind": "supported", "value": 5}}})
    with pytest.raises(ValueError, match="满分"):
        final_review.submit(project, review_id, "agent", over_max, "核查Agent")
    not_a_band = pass_items(project, review_id, "agent", "核查Agent", {"CHK-S001": {"score": {"kind": "supported", "value": 2}}})
    with pytest.raises(ValueError, match="档位"):
        final_review.submit(project, review_id, "agent", not_a_band, "核查Agent")

    valid = pass_items(project, review_id, "agent", "核查Agent", {
        "CHK-S001": {"score": {"kind": "supported", "value": 3}},
        "CHK-T001": {"score": {"kind": "simulated", "value": 9}},
        "CHK-P001": {"score": {"kind": "unknown"}},
    })
    final_review.submit(project, review_id, "agent", valid, "核查Agent")
    final_review.prepare(project, review_id, "human")
    submit_pass(project, review_id, "human", "人工复核人")
    state = final_review.finalize(project, review_id)
    assert state["status"] == "content_review_passed"
    assert state["scores"]["agent"]["supported"] == 3
    assert state["scores"]["agent"]["simulated"] == 9, "技术模拟分单独列示"
    assert state["scores"]["agent"]["unknown"] == ["P001"]
    assert state["closure_required"] == [], "纯评分差异保留条件分，不按废标阻断"


def test_source_local_copy_and_rules_changes_invalidate_previous_pass(tmp_path):
    project = make_project(tmp_path)
    pdf = make_pdf(tmp_path / "输入.pdf", pages=1)
    review = final_review.start(project, pdfs=[pdf], writer="writer-A")
    review_id = review["id"]
    for lane in ("agent", "human"):
        final_review.prepare(project, review_id, lane)
    submit_pass(project, review_id, "agent", "核查Agent")
    submit_pass(project, review_id, "human", "人工复核人")
    assert final_review.finalize(project, review_id)["status"] == "content_review_passed"

    # 外部原件仍在原位置但内容被改动：旧通过不得沿用。
    pdf.write_bytes(b"%PDF-1.4 changed external original")
    state = final_review.status(project, review_id)
    assert state["status"] == "stale" and any("外部原始PDF已变化" in reason for reason in state["reasons"])
    with pytest.raises(ValueError, match="已变化"):
        final_review.submit(project, review_id, "agent", pass_items(project, review_id, "agent", "核查Agent"), "核查Agent")

    # 本地锁定副本被改动同样失效。
    fresh_pdf = make_pdf(tmp_path / "输入2.pdf", pages=1)
    second = final_review.start(project, pdfs=[fresh_pdf], writer="writer-A")
    second_id = second["id"]
    for lane in ("agent", "human"):
        final_review.prepare(project, second_id, lane)
    submit_pass(project, second_id, "agent", "核查Agent")
    submit_pass(project, second_id, "human", "人工复核人")
    assert final_review.finalize(project, second_id)["status"] == "content_review_passed"
    project.safe_path(second["inputs"][0]["path"]).write_bytes(b"%PDF-1.4 changed local copy")
    assert final_review.status(project, second_id)["status"] == "stale"

    # 规则更新后旧通过失效，未提交的结果也被拒绝。
    third_pdf = make_pdf(tmp_path / "输入3.pdf", pages=1)
    third = final_review.start(project, pdfs=[third_pdf], writer="writer-A")
    third_id = third["id"]
    final_review.prepare(project, third_id, "agent")
    rules = project.load("rules")
    rules[0]["text"] = "法人证照要求已更新（合成测试）"
    project.save("rules", rules, reason="合成测试：更新规则")
    with pytest.raises(ValueError, match="已变化"):
        final_review.submit(project, third_id, "agent", pass_items(project, third_id, "agent", "核查Agent"), "核查Agent")
    assert final_review.finalize(project, third_id)["status"] == "stale"


def test_lane_disagreement_is_preserved_and_can_be_corrected_same_version(tmp_path):
    project = make_project(tmp_path)
    pdf = make_pdf(tmp_path / "disagree.pdf", pages=1)
    review_id = final_review.start(project, pdfs=[pdf], writer="writer-A")["id"]
    for lane in ("agent", "human"):
        final_review.prepare(project, review_id, lane)
    submit_pass(project, review_id, "agent", "核查Agent")
    submit_pass(project, review_id, "human", "人工复核人", {
        "CHK-Q001": {"status": "risk", "reason": "人工初看认为证照页缺失"},
    })
    state = final_review.finalize(project, review_id)
    assert state["status"] == "blocked"
    assert any(row["item_id"] == "CHK-Q001" for row in state["disagreements"])
    report = project.safe_path(state["report"]["markdown"]).read_text(encoding="utf-8")
    assert "人工初看认为证照页缺失" in report

    # 同版本更正误报：新revision保留原风险结论。
    submit_pass(project, review_id, "human", "人工复核人", {
        "CHK-Q001": {"status": "pass", "reason": "复核原页后更正：证照页实际在第1页，前次误报"},
    })
    state = final_review.finalize(project, review_id)
    assert state["status"] == "content_review_passed"
    revisions = [row for row in final_review._get(project, review_id)["revisions"] if row["lane"] == "human"]
    assert len(revisions) == 2
    first_q = next(item for item in revisions[0]["items"] if item["id"] == "CHK-Q001")
    assert first_q["status"] == "risk"


def test_signed_requires_passed_parent_and_compares_content(tmp_path):
    project = make_project(tmp_path)
    content_pdf = make_pdf(tmp_path / "待签章内容.pdf", pages=2, marker="CONTENT")
    review_id = final_review.start(project, pdfs=[content_pdf], writer="writer-A")["id"]
    for lane in ("agent", "human"):
        final_review.prepare(project, review_id, lane)
    submit_pass(project, review_id, "agent", "核查Agent")
    submit_pass(project, review_id, "human", "人工复核人")
    assert final_review.finalize(project, review_id)["status"] == "content_review_passed"

    signed_pdf = make_pdf(tmp_path / "已签章.pdf", pages=2, marker="SIGNED")
    with pytest.raises(ValueError, match="parent"):
        final_review.start(project, pdfs=[signed_pdf], stage="signed", writer="writer-A")
    with pytest.raises(ValueError, match="找不到"):
        final_review.start(project, pdfs=[signed_pdf], stage="signed", writer="writer-A", parent="FR999")
    # 用未通过的另一份内容记录作parent也拒绝。
    other_pdf = make_pdf(tmp_path / "另一份内容.pdf", pages=1, marker="OTHER")
    other = final_review.start(project, pdfs=[other_pdf], writer="writer-A")["id"]
    with pytest.raises(ValueError, match="尚未通过或已失效"):
        final_review.start(project, pdfs=[signed_pdf], stage="signed", writer="writer-A", parent=other)

    signed_id = complete_signed(project, review_id, [signed_pdf])
    state = final_review.finalize(project, signed_id)
    assert state["status"] == "signed_review_passed"
    signed_inputs = final_review._get(project, signed_id)["inputs"]
    assert signed_inputs[0]["sha256"] == sha256_file(signed_pdf)
    # content记录不能冒充signed：提交前检查只对signed阶段开放。
    with pytest.raises(ValueError, match="signed阶段"):
        final_review.submission(project, review_id, {"review_id": review_id, "actor": "递交确认人",
                                                     "items": [{"id": "SUB-01", "status": "confirmed", "reason": "合成测试"}]},
                                "递交确认人", attest_human=True)

    # 签章版内容对比风险必须阻断。
    second_signed = make_pdf(tmp_path / "已签章2.pdf", pages=2, marker="SIGNED2")
    second_id = final_review.start(project, pdfs=[second_signed], stage="signed", writer="writer-A", parent=review_id)["id"]
    for lane in ("agent", "human"):
        final_review.prepare(project, second_id, lane)
    submit_pass(project, second_id, "agent", "签章核查Agent", {
        "CHK-STAGE-SIGN-COMPARE": {"status": "risk", "reason": "第2页正文被改动，不只多了章"},
    })
    submit_pass(project, second_id, "human", "签章人工复核人")
    state = final_review.finalize(project, second_id)
    assert state["status"] == "blocked" and any(row["item_id"] == "CHK-STAGE-SIGN-COMPARE" for row in state["blocking"])


def test_refresh_status_recomputes_live_and_tampered_copy_is_stale(tmp_path):
    project = make_project(tmp_path)
    pdf = make_pdf(tmp_path / "live.pdf", pages=1)
    review = final_review.start(project, pdfs=[pdf], writer="writer-A")
    review_id = review["id"]
    for lane in ("agent", "human"):
        final_review.prepare(project, review_id, lane)
    submit_pass(project, review_id, "agent", "核查Agent")
    submit_pass(project, review_id, "human", "人工复核人")
    assert final_review.finalize(project, review_id)["status"] == "content_review_passed"
    assert project.refresh_status()["final_reviews"][0]["status"] == "content_review_passed"

    project.safe_path(review["inputs"][0]["path"]).write_bytes(b"changed local locked copy")
    refreshed = project.refresh_status()
    row = next(item for item in refreshed["final_reviews"] if item["id"] == review_id)
    assert row["status"] == "stale", "JSON必须使用实时evaluate，不得回退缓存通过状态"
    assert "stale" in project.safe_path("项目状态.md").read_text(encoding="utf-8")
    from bidflow.workflow import next_steps
    assert any("输入已变化" in action for action in next_steps(project)["next"])


def test_signed_compare_disagreement_requires_closure(tmp_path):
    project = make_project(tmp_path)
    content_pdf = make_pdf(tmp_path / "content2.pdf", pages=1, marker="CONTENT2")
    content_id = final_review.start(project, pdfs=[content_pdf], writer="writer-A")["id"]
    for lane in ("agent", "human"):
        final_review.prepare(project, content_id, lane)
    submit_pass(project, content_id, "agent", "核查Agent")
    submit_pass(project, content_id, "human", "人工复核人")
    assert final_review.finalize(project, content_id)["status"] == "content_review_passed"

    signed_pdf = make_pdf(tmp_path / "signed2.pdf", pages=1, marker="SIGNED2")
    signed_id = final_review.start(project, pdfs=[signed_pdf], stage="signed", writer="writer-A", parent=content_id)["id"]
    for lane in ("agent", "human"):
        final_review.prepare(project, signed_id, lane)
    submit_pass(project, signed_id, "agent", "签章核查Agent", {"CHK-STAGE-SIGN-COMPARE": {"status": "not_applicable", "reason": "Agent认为无需逐页对比（需解释）"}})
    submit_pass(project, signed_id, "human", "签章人工复核人")
    state = final_review.finalize(project, signed_id)
    assert state["status"] == "blocked"
    assert any(row["item_id"] == "CHK-STAGE-SIGN-COMPARE" for row in state["closure_required"])
    assert any("结论不一致" in reason for reason in state["reasons"])


def test_submission_checklist_is_item_by_item_and_never_claims_submitted(tmp_path):
    project = make_project(tmp_path)
    content_pdf = make_pdf(tmp_path / "content.pdf", pages=1, marker="CONTENT")
    content_id = final_review.start(project, pdfs=[content_pdf], writer="writer-A")["id"]
    for lane in ("agent", "human"):
        final_review.prepare(project, content_id, lane)
    submit_pass(project, content_id, "agent", "核查Agent")
    submit_pass(project, content_id, "human", "人工复核人")
    final_review.finalize(project, content_id)
    signed_pdf = make_pdf(tmp_path / "signed.pdf", pages=1, marker="SIGNED")
    signed_id = complete_signed(project, content_id, [signed_pdf])

    with pytest.raises(ValueError, match="attest-human"):
        final_review.submission(project, signed_id, {"review_id": signed_id, "actor": "递交确认人",
                                                     "items": [{"id": "SUB-01", "status": "confirmed", "reason": "合成测试"}]},
                                "递交确认人")
    partial = confirm_submission(project, signed_id, statuses={"SUB-03": "pending", "SUB-04": "pending"})
    assert partial["status"] == "submission_check_recorded"
    assert {item["id"] for item in partial["submission_items"] if item["status"] == "pending"} == {"SUB-03", "SUB-04"}
    ready = confirm_submission(project, signed_id)
    assert ready["status"] == "ready_for_submission"
    assert "不代表已经提交" in ready["status_text"] and "不保证不废标" in ready["status_text"]
    assert final_review.status(project, signed_id)["status"] == "ready_for_submission"


def test_pdf_cache_reuse_and_context_budget_batching(tmp_path):
    project = make_project(tmp_path)
    pdf = make_pdf(tmp_path / "缓存.pdf", pages=1, marker="UNIQUEPDFMARKER12345")
    first = final_review.start(project, pdfs=[pdf], writer="writer-A")
    assert first["cache"][0]["cache_hit"] is False
    cache_file = project.safe_path(first["cache"][0]["cache"])
    assert "UNIQUEPDFMARKER12345" in cache_file.read_text(encoding="utf-8")
    second = final_review.start(project, pdfs=[pdf], writer="writer-A")
    assert second["cache"][0]["cache_hit"] is True, "同一PDF第二次启动必须复用逐页缓存"
    final_review.prepare(project, second["id"], "agent")
    context = project.safe_path(f"{second['folder']}/agent/B01/context.json").read_text(encoding="utf-8")
    assert "UNIQUEPDFMARKER12345" not in context, "任务包不得塞入整本PDF全文"
    assert "structure.json" in context

    # 长规则超预算时自动分批，不会静默截断；单条规则都放不下时明确拒绝。
    rules = project.load("rules")
    for rule in rules:
        rule["text"] = rule["text"] + "补充说明" * 400
    project.save("rules", rules, reason="合成测试：长规则")
    from bidflow.workflow import confirm
    confirm(project, "rules", "规则确认人", attest_human=True)
    settings = project.load("settings")
    settings["context_budget"] = 9000
    project.save("settings", settings)
    third = final_review.start(project, pdfs=[pdf], writer="writer-A")
    prepared = final_review.prepare(project, third["id"], "agent")
    assert len(prepared["batches"]) >= 2, "超预算必须拆成多个规则批次"
    total_items = []
    for batch in prepared["batches"]:
        path = project.safe_path(batch["context"])
        assert len(path.read_text(encoding="utf-8")) <= 9001, "分批后每个任务包仍须在保守预算内"
        total_items.extend(batch["check_item_ids"])
    assert sorted(total_items) == sorted(item["id"] for item in final_review._get(project, third["id"])["check_items"])

    rules[0]["text"] = "超长规则" * 8000
    project.save("rules", rules, reason="合成测试：单条超预算")
    confirm(project, "rules", "规则确认人", attest_human=True)
    fourth = final_review.start(project, pdfs=[pdf], writer="writer-A")
    with pytest.raises(ValueError, match="预算"):
        final_review.prepare(project, fourth["id"], "agent")
    # 人工清单不受模型预算限制，仍可整表生成。
    assert final_review.prepare(project, fourth["id"], "human")["checklist"]


def test_assembly_gate_matches_bytes_and_survives_path_change(tmp_path):
    project = make_project(tmp_path)
    assembly_pdf = project.safe_path("06_审核检查/组卷预览/RUN1/投标文件.pdf")
    assembly_pdf.parent.mkdir(parents=True, exist_ok=True)
    make_pdf(assembly_pdf, pages=1, marker="ASSEMBLY")
    project.save("assembly", {"rendered": True, "run_id": "RUN1",
                              "outputs": [{"volume": "投标文件", "pdf": assembly_pdf.relative_to(project.root).as_posix(), "docx": "x"}]})
    content_id = complete_content(project, writer="writer-A")
    gate = final_review.assembly_gate(project)
    assert gate["ok"] and gate["review_id"] == content_id

    # ready 复制只改变路径不改变字节：同一份双审仍然有效。
    copied = project.safe_path("07_最终输出/RUN1/投标文件.pdf")
    copied.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(assembly_pdf, copied)
    project.save("assembly", {"rendered": True, "run_id": "RUN1",
                              "outputs": [{"volume": "投标文件", "pdf": copied.relative_to(project.root).as_posix(), "docx": "x"}]})
    assert final_review.assembly_gate(project)["ok"], "路径变化但字节相同不得使双审失效"

    other = make_pdf(tmp_path / "other.pdf", pages=1, marker="OTHER")
    project.save("assembly", {"rendered": True, "run_id": "RUN1",
                              "outputs": [{"volume": "投标文件", "pdf": other.relative_to(tmp_path).as_posix(), "docx": "x"}]})
    # 外部路径不属于项目，应该被 safe_path 拒绝或门禁不通过。
    assert not final_review.assembly_gate(project)["ok"]


def test_submit_rejects_stale_read_race_and_keeps_other_lane(tmp_path, monkeypatch):
    """并发窗口回归：_get返回旧review的瞬间另一lane合法提交，当前提交必须版本冲突且不覆盖对方。"""
    project = make_project(tmp_path)
    pdf = make_pdf(tmp_path / "race.pdf", pages=1)
    review_id = final_review.start(project, pdfs=[pdf], writer="writer-A")["id"]
    for lane in ("agent", "human"):
        final_review.prepare(project, review_id, lane)

    real_get = final_review._get
    injected = {"done": False}
    agent_result = pass_items(project, review_id, "agent", "核查Agent")

    def racy_get(target_project, target_id):
        row = real_get(target_project, target_id)
        if target_id == review_id and not injected["done"]:
            injected["done"] = True
            # 模拟另一条lane在本流程捕获指纹之后、真正使用记录之前完成一次合法提交。
            final_review.submit(project, review_id, "human",
                                pass_items(project, review_id, "human", "人工复核人"),
                                "人工复核人", attest_human=True)
        return row

    monkeypatch.setattr(final_review, "_get", racy_get)
    with pytest.raises(RuntimeError, match="已被另一条lane或其他操作更新"):
        final_review.submit(project, review_id, "agent", agent_result, "核查Agent")
    stored = final_review._get(project, review_id)
    assert [row["lane"] for row in stored["revisions"]] == ["human"], "当前提交不得用旧快照覆盖另一lane结果"
    assert stored["revisions"][0]["actor"] == "人工复核人"
    assert not any(row["lane"] == "agent" for row in stored["revisions"]), "被拒绝的提交不得写入历史"


@pytest.mark.parametrize("action", ["start", "prepare", "finalize", "submission"])
def test_all_mutations_capture_fingerprint_before_reading_record(tmp_path, monkeypatch, action):
    """结构回归：start/prepare/finalize/submission 的 final_reviews 乐观指纹必须先于记录读取。"""
    project = make_project(tmp_path)
    pdf = make_pdf(tmp_path / f"顺序-{action}.pdf", pages=1)
    review_id = None if action == "start" else final_review.start(project, pdfs=[pdf], writer="writer-A")["id"]

    order = []
    real_fingerprint = type(project).fingerprint
    real_get, real_reviews = final_review._get, final_review._reviews

    def spy_fingerprint(self, names):
        if "final_reviews" in names:
            order.append("fingerprint")
        return real_fingerprint(self, names)

    monkeypatch.setattr(type(project), "fingerprint", spy_fingerprint)
    monkeypatch.setattr(final_review, "_get", lambda target_project, target_id: (order.append("read"), real_get(target_project, target_id))[1])
    monkeypatch.setattr(final_review, "_reviews", lambda target_project: (order.append("read"), real_reviews(target_project))[1])

    if action == "start":
        final_review.start(project, pdfs=[pdf], writer="writer-A")
    elif action == "prepare":
        final_review.prepare(project, review_id, "agent")
    elif action == "finalize":
        final_review.finalize(project, review_id)
    else:
        # content记录会被拒绝，但顺序证明：修复前这里只读记录、根本不会捕获指纹。
        with pytest.raises(ValueError, match="signed"):
            final_review.submission(project, review_id,
                                    {"review_id": review_id, "actor": "递交确认人",
                                     "items": [{"id": "SUB-01", "status": "confirmed", "reason": "合成测试"}]},
                                    "递交确认人", attest_human=True)

    assert order, f"{action} 必须实际读写final_reviews"
    assert order[0] == "fingerprint", f"{action} 必须先在读取前捕获乐观指纹，实际顺序：{order}"


def _passed_content_review(tmp_path):
    """合成项目完成一次外部PDF内容双审并取得通过，供前置条件失效回归使用。"""
    project = make_project(tmp_path)
    pdf = make_pdf(tmp_path / "前置条件.pdf", pages=1, marker="LIVE GATE")
    review_id = final_review.start(project, pdfs=[pdf], writer="writer-A")["id"]
    for lane in ("agent", "human"):
        final_review.prepare(project, review_id, lane)
    submit_pass(project, review_id, "agent", "核查Agent")
    submit_pass(project, review_id, "human", "人工复核人")
    assert final_review.finalize(project, review_id)["status"] == "content_review_passed"
    return project, review_id


def _assert_relapse_invalidates_all_entries(project, review_id, fragment):
    """前置条件失效后：status/finalize/refresh_status同口径，prepare/submit拒绝，旧passed不保留。"""
    state = final_review.status(project, review_id)
    assert state["status"] != "content_review_passed", "旧通过状态不得保留"
    assert any(fragment in reason for reason in state["reasons"]), state["reasons"]
    with pytest.raises(ValueError, match=fragment):
        final_review.prepare(project, review_id, "agent")
    with pytest.raises(ValueError, match=fragment):
        final_review.submit(project, review_id, "agent"
                            , pass_items(project, review_id, "agent", "核查Agent"), "核查Agent")
    final = final_review.finalize(project, review_id)
    assert final["status"] == state["status"], "finalize必须与status同口径"
    assert final["status"] != "content_review_passed"
    assert final_review._get(project, review_id)["status"] == state["status"], "旧passed状态不得写回保留"
    refreshed = project.refresh_status()
    row = next(item for item in refreshed["final_reviews"] if item["id"] == review_id)
    assert row["status"] == state["status"], "refresh_status必须与finalize同口径"


def test_new_open_rule_conflict_invalidates_every_status_entry(tmp_path):
    """新增未解决规则冲突后，已通过的内容双审立即失效，不能继续prepare/submit或保留passed。"""
    project, review_id = _passed_content_review(tmp_path)
    project.save("issues", [{"id": "ISSUE-LIVE-1", "category": "规则冲突", "severity": "error",
                             "status": "open", "message": "合成测试：新增规则冲突"}], reason="合成测试：新增规则冲突")
    _assert_relapse_invalidates_all_entries(project, review_id, "规则冲突")


def test_withdrawn_rules_confirmation_invalidates_every_status_entry(tmp_path):
    """撤回人工规则确认后，已通过的内容双审立即失效，不能继续prepare/submit或保留passed。"""
    project, review_id = _passed_content_review(tmp_path)
    project.save("confirmations", [], reason="合成测试：撤回人工规则确认")
    _assert_relapse_invalidates_all_entries(project, review_id, "rules确认")
