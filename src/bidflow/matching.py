"""保守的证据核验、确定性计分、人员配置及审核报告。

本模块只读取主记录并生成派生报告，不把检索命中提升为已证实结论。
"""

from __future__ import annotations

import itertools
import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .utils import atomic_json, json_hash, sha256_file, utc_now, write_text


def _number(value: Any, default: float = 0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    normalized = re.sub(r"[年月/.]", "-", str(value)).replace("日", "")
    try:
        return date.fromisoformat(normalized[:10])
    except ValueError:
        return None


def _context(project) -> dict:
    names = ("files", "blocks", "rules", "evidence", "responses", "staff", "history", "sections", "reviews", "plans", "forms")
    result = {name: project.load(name, []) for name in names}
    result.update({name: project.load(name, {}) for name in ("facts", "settings", "assembly")})
    result["manual_checks"] = project.load("manual_checks", [])
    result["files_by_id"] = {row["id"]: row for row in result["files"]}
    result["blocks_by_id"] = {row["id"]: row for row in result["blocks"]}
    result["evidence_by_id"] = {row["id"]: row for row in result["evidence"]}
    result["sections_by_id"] = {row["id"]: row for row in result["sections"]}
    result["forms_by_id"] = {row["id"]: row for row in result["forms"]}
    result["manual_by_id"] = {row["id"]: row for row in result["manual_checks"]}
    result["file_checks"] = {}
    result["project"] = project
    return result


def _check_file(ctx: dict, file_id: str) -> list[str]:
    if file_id in ctx["file_checks"]:
        return ctx["file_checks"][file_id]
    row = ctx["files_by_id"].get(file_id)
    problems = []
    if not row:
        problems.append("文件未登记")
    else:
        try:
            path = ctx["project"].safe_path(row.get("path", ""))
            if not path.is_file():
                problems.append("原始文件不存在")
            elif not row.get("sha256") or sha256_file(path) != row["sha256"]:
                problems.append("原始文件已变化或缺少文件哈希，需重新导入核验")
        except (OSError, ValueError):
            problems.append("文件路径不可访问或超出项目范围")
    ctx["file_checks"][file_id] = problems
    return problems


def _check_rule(ctx: dict, rule: dict) -> list[str]:
    problems = []
    if rule.get("status") != "confirmed":
        problems.append("规则尚未确认")
    if rule.get("conflict"):
        problems.append("规则存在尚未解决的冲突")
    sources = rule.get("sources", [])
    if not sources:
        problems.append("规则缺少原文来源")
    for source in sources:
        problems.extend(_check_file(ctx, source.get("file_id", "")))
        block = ctx["blocks_by_id"].get(source.get("block_id"))
        if not block or block.get("file_id") != source.get("file_id"):
            problems.append("规则来源段落不存在或文件不一致")
        elif not source.get("quote") or source["quote"] not in block.get("text", ""):
            problems.append("规则原文引文无法在来源段落定位")
        elif block.get("quality") == "review" and not source.get("visually_verified"):
            problems.append("规则来源识别质量待核对")
    return list(dict.fromkeys(problems))


def _check_evidence(ctx: dict, evidence: dict, rule: dict | None = None, response: dict | None = None) -> list[str]:
    problems = list(_check_file(ctx, evidence.get("file_id", "")))
    file_row = ctx["files_by_id"].get(evidence.get("file_id"), {})
    if file_row.get("category") in ("业绩台账", "方法论", "历史章节"):
        problems.append("台账、方法论及历史章节只作参考，不能直接作为正式证明材料")
    if evidence.get("verification") != "verified":
        problems.append("证明材料尚未核验通过")
    if not evidence.get("verified_by"):
        problems.append("证明材料缺少核验人记录")
    if not evidence.get("file_sha256") or evidence.get("file_sha256") != file_row.get("sha256"):
        problems.append("证据核验时的文件版本不一致")
    pages = evidence.get("pages", [])
    if not pages or any(not isinstance(page, int) or isinstance(page, bool) or page < 1 for page in pages):
        problems.append("证明材料缺少有效证明页")
    known_pages = file_row.get("page_count")
    if known_pages and any(isinstance(page, int) and page > known_pages for page in pages):
        problems.append("证明页超出原始文件页数")
    poor_blocks = [block for block in ctx["blocks"] if block.get("file_id") == evidence.get("file_id") and block.get("quality") == "review" and (not block.get("page") or block["page"] in pages)]
    if (poor_blocks or file_row.get("parse_status") in ("failed", "error", "pending")) and not evidence.get("visually_verified"):
        problems.append("证明页识别质量待核对，须记录对原页的人工核验")
    facts = evidence.get("facts", {})
    if not facts:
        problems.append("证明材料未记录能够证明的事实")
    criteria = (rule or {}).get("criteria", {})
    expiry_value = facts.get("valid_until", facts.get("expiry_date", facts.get("valid_to")))
    start_value = facts.get("valid_from")
    deadline = _date(ctx["facts"].get("deadline"))
    if expiry_value or start_value or criteria.get("expiration_required"):
        if not deadline:
            problems.append("投标截止日期尚未确认，无法核验证书有效期")
        if expiry_value and not _date(expiry_value):
            problems.append("证明材料有效期格式无法核验")
        elif expiry_value and deadline and _date(expiry_value) < deadline:
            problems.append("证明材料在投标截止日已过期")
        if start_value and not _date(start_value):
            problems.append("证明材料生效日期格式无法核验")
        elif start_value and deadline and _date(start_value) > deadline:
            problems.append("证明材料在投标截止日尚未生效")
        if criteria.get("expiration_required") and not expiry_value and not facts.get("permanent_validity"):
            problems.append("证明材料缺少有效期或长期有效的核验事实")
    personnel = criteria.get("personnel", {})
    required_role = personnel.get("required_role", criteria.get("required_role"))
    required_person = (response or {}).get("staff_id") or personnel.get("staff_id")
    if required_person:
        proven_people = {facts.get("staff_id"), *(item.get("staff_id") for item in facts.get("person_roles", []))}
        if required_person not in proven_people:
            problems.append("材料未证明响应中指定的人员，不能借用其他人员证据")
    expected_project = (response or {}).get("project_id")
    if expected_project and expected_project not in {facts.get("project_id"), *(item.get("project_id") for item in facts.get("person_roles", []))}:
        problems.append("证明材料项目与响应项目不一致")
    if required_role:
        if not required_person:
            problems.append("人员业绩缺少明确的人员ID")
        else:
            roles = facts.get("person_roles", [])
            if facts.get("staff_id"):
                roles = [*roles, {"staff_id": facts["staff_id"], "role": facts.get("role"), "project_id": facts.get("project_id")}]
            equivalent = personnel.get("roles_equivalent", criteria.get("roles_equivalent", []))
            acceptable = {required_role, *equivalent}
            project_id = (response or {}).get("project_id")
            valid = any(row.get("staff_id") == required_person and row.get("role") in acceptable and (not project_id or row.get("project_id") == project_id) for row in roles)
            if not valid:
                problems.append(f"材料未证明指定人员承担“{required_role}”角色；参与经历不能替代主持业绩")
    return list(dict.fromkeys(problems))


def _check_section(ctx: dict, section_id: str) -> list[str]:
    section = ctx["sections_by_id"].get(section_id)
    if not section:
        return ["响应正文章节不存在"]
    problems = []
    if section.get("status") != "confirmed":
        problems.append("正文章节尚未确认或已失效")
    try:
        path = ctx["project"].safe_path(section.get("path", ""))
        if not path.is_file() or not section.get("sha256") or sha256_file(path) != section["sha256"]:
            problems.append("正文章节已变化或文件不存在，需重新确认")
    except (OSError, ValueError):
        problems.append("正文章节路径无效")
    return problems


def _check_form(ctx: dict, form_id: str) -> list[str]:
    form = ctx["forms_by_id"].get(form_id)
    if not form:
        return ["响应表单不存在"]
    problems = []
    if form.get("status") != "confirmed" or not form.get("template_approved"):
        problems.append("响应表单或招标模板尚未确认")
    if form.get("errors") or form.get("missing_fields"):
        problems.append("响应表单仍有填充错误或缺失字段")
    for field, hash_field in (("template_path", "template_sha256"), ("output_path", "output_sha256")):
        try:
            path = ctx["project"].safe_path(form.get(field, ""))
            if not path.is_file() or not form.get(hash_field) or sha256_file(path) != form[hash_field]:
                problems.append(f"表单 {field} 文件不存在、已变化或缺少版本记录")
        except (OSError, ValueError):
            problems.append("响应表单路径无法核验")
    if not form.get("source_refs"):
        problems.append("表单未建立招标模板来源定位")
    for source in form.get("source_refs", []):
        if not isinstance(source, dict):
            problems.append("表单来源定位格式无效")
            continue
        problems.extend(_check_file(ctx, source.get("file_id", "")))
        block = ctx["blocks_by_id"].get(source.get("block_id"))
        if not block or block.get("file_id") != source.get("file_id"):
            problems.append("表单来源段落不存在或文件不一致")
    values = {**ctx["facts"], **form.get("fields", {})}
    for key in form.get("required_fields", []):
        if values.get(key) is None or not str(values[key]).strip():
            problems.append(f"表单必填字段 {key} 缺失")
    return list(dict.fromkeys(problems))


def _check_manual(ctx: dict, check_id: str) -> list[str]:
    item = ctx["manual_by_id"].get(check_id)
    if not item or item.get("status") != "confirmed" or not item.get("actor"):
        return ["关联人工检查项尚未确认或缺少确认人"]
    if item.get("dependencies") and item.get("fingerprint") != ctx["project"].fingerprint(item["dependencies"]):
        return ["人工检查对应的输入版本已变化，需重新确认"]
    return []


def _response(ctx: dict, rule: dict, response: dict) -> dict:
    problems = []
    if response.get("status") != "supported":
        problems.append("响应的本次规则适用性尚未核验通过")
    if response.get("rule_revision") != rule.get("revision"):
        problems.append("响应对应旧版规则，需按补遗重新核验")
    if not response.get("rationale"):
        problems.append("响应缺少本次规则适用性说明")
    evidence_rows, section_rows, form_rows, manual_rows = [], [], [], []
    for evidence_id in dict.fromkeys(response.get("evidence_ids", [])):
        evidence = ctx["evidence_by_id"].get(evidence_id)
        notes = _check_evidence(ctx, evidence, rule, response) if evidence else ["证明材料记录不存在"]
        evidence_rows.append({"id": evidence_id, "valid": not notes, "problems": notes, "intrinsic_valid": bool(evidence) and not _check_evidence(ctx, evidence)})
    for section_id in dict.fromkeys(response.get("section_ids", [])):
        notes = _check_section(ctx, section_id)
        section_rows.append({"id": section_id, "valid": not notes, "problems": notes})
    for form_id in dict.fromkeys(response.get("form_ids", [])):
        notes = _check_form(ctx, form_id)
        form_rows.append({"id": form_id, "valid": not notes, "problems": notes})
    for check_id in dict.fromkeys(response.get("manual_check_ids", [])):
        notes = _check_manual(ctx, check_id)
        manual_rows.append({"id": check_id, "valid": not notes, "problems": notes})
    proof_mode = rule.get("criteria", {}).get("proof_mode") or ("section" if rule["kind"] == "technical" else "either" if rule["kind"] in ("invalid", "requirement") else "evidence")
    evidence_ok = any(row["valid"] for row in evidence_rows)
    section_ok = any(row["valid"] for row in section_rows)
    form_ok = any(row["valid"] for row in form_rows)
    manual_ok = any(row["valid"] for row in manual_rows)
    # either 接受可组卷的材料、正文或表单；纯人工事项必须显式选 manual。
    proof_ok = {"evidence": evidence_ok, "section": section_ok, "form": form_ok, "manual": manual_ok, "either": evidence_ok or section_ok or form_ok, "both": evidence_ok and (section_ok or form_ok)}.get(proof_mode, False)
    if not proof_ok:
        problems.append("缺少满足规则证明方式的有效材料或确认正文")
    if rule.get("criteria", {}).get("all_evidence_required") and (not evidence_rows or any(not row["intrinsic_valid"] for row in evidence_rows)):
        problems.append("规则要求的整组证明材料尚未全部有效")
    if rule.get("criteria", {}).get("manual_required") and not manual_ok:
        problems.append("本项要求必须完成关联人工检查")
    if response.get("missing"):
        problems.append("响应仍有记录在案的待补事项")
    return {"id": response.get("id"), "staff_id": response.get("staff_id"), "project_id": response.get("project_id"), "valid": not problems, "problems": problems, "evidence": evidence_rows, "sections": section_rows, "forms": form_rows, "manual_checks": manual_rows, "missing": response.get("missing", []), "covered_subrequirements": response.get("covered_subrequirements", [])}


def _evaluate(ctx: dict, rule: dict, selected_staff: set[str] | None = None) -> dict:
    responses = [row for row in ctx["responses"] if row.get("rule_id") == rule["id"] and (selected_staff is None or not row.get("staff_id") or row["staff_id"] in selected_staff)]
    results = [_response(ctx, rule, row) for row in responses]
    problems = _check_rule(ctx, rule)
    if not results:
        problems.append("尚未建立响应关系")
    elif not any(row["valid"] for row in results):
        problems.append("现有响应尚无有效证明支持")
        problems.extend(problem for row in results for problem in row["problems"])
    covered = {item for row in results if row["valid"] for item in row["covered_subrequirements"]}
    missing_subrequirements = [item for item in rule.get("subrequirements", []) if item not in covered]
    if missing_subrequirements:
        problems.append("尚未覆盖全部评分或响应子要求")
    minimum = rule.get("criteria", {}).get("min_count")
    if minimum is not None:
        keys = {_item_key(ctx["evidence_by_id"][evidence["id"]], rule.get("criteria", {})) for row in results if row["valid"] for evidence in row["evidence"] if evidence["valid"]}
        keys.discard(None)
        if _number(minimum, -1) < 0 or len(keys) < _number(minimum):
            problems.append("有效且去重后的证明项数未达到规则要求")
    return {"rule_id": rule["id"], "kind": rule["kind"], "title": rule.get("title", ""), "revision": rule.get("revision"), "supported": not problems, "problems": problems, "missing_subrequirements": missing_subrequirements, "responses": results, "sources": rule.get("sources", [])}


def _match(ctx: dict, selected_staff: set[str] | None = None) -> dict:
    rows = [_evaluate(ctx, rule, selected_staff) for rule in ctx["rules"] if rule.get("status") != "retired"]
    candidates = []
    for rule in ctx["rules"]:
        if rule.get("status") == "retired":
            continue
        # 候选条件必须由已拆解规则提供，检索命中只列为待核验线索。
        filters = rule.get("criteria", {}).get("candidate_filter", {})
        if not filters:
            continue
        for item in ctx["history"]:
            person = filters.get("staff_id")
            if person and not any(role.get("staff_id") == person for role in item.get("roles", [])):
                continue
            if filters.get("project_type") and item.get("facts", {}).get("project_type") != filters["project_type"]:
                continue
            if filters.get("from_date"):
                start = _date(filters["from_date"])
                if start is None:
                    raise ValueError(f"规则 {rule['id']} 的候选起始日期格式无效")
                if not _date(item.get("contract_date")) or _date(item["contract_date"]) < start:
                    continue
            candidates.append({"rule_id": rule["id"], "project_id": item["id"], "project_name": item.get("name"), "contract_no": item.get("contract_no"), "roles": item.get("roles", []), "status": "candidate", "note": "台账线索，需核验原始证明材料及本次招标适用性"})
    missing = []
    for row in rows:
        if row["supported"]:
            continue
        clues = [item for item in candidates if item["rule_id"] == row["rule_id"]]
        details = [note for response in row["responses"] for item in response["evidence"] + response["sections"] + response["forms"] + response["manual_checks"] for note in item["problems"]]
        declared = [note for response in row["responses"] for note in response["missing"]]
        criteria = next(rule.get("criteria", {}) for rule in ctx["rules"] if rule["id"] == row["rule_id"])
        mode = criteria.get("proof_mode", "section" if row["kind"] == "technical" else "either" if row["kind"] in ("invalid", "requirement") else "evidence")
        missing.append({"rule_id": row["rule_id"], "kind": row["kind"], "title": row["title"], "proof_mode": mode, "needed": list(dict.fromkeys(row["problems"] + row["missing_subrequirements"] + details + declared)), "candidates": clues})
    return {"status": "review_required" if missing else "supported", "summary": f"已核查 {len(rows)} 项要求，{sum(row['supported'] for row in rows)} 项有当前有效响应。", "rules": rows, "candidates": candidates, "missing": missing}


def match(project) -> dict:
    """核验已有适用性判断，返回台账候选及缺件，绝不自动认定类似业绩。"""
    return _match(_context(project))


def _scoring(rule: dict) -> dict:
    criteria = rule.get("criteria", {})
    return criteria.get("scoring", criteria)


def _item_key(evidence: dict, config: dict) -> str | None:
    field = config.get("distinct_by", "file_id")
    if field in ("id", "file_id", "file_sha256"):
        # 同一份材料新建证据编号或文件别名，均不能制造额外项数。
        field, value = "file_sha256", evidence.get("file_sha256")
    else:
        value = evidence.get("facts", {}).get(field)
    return f"{field}:{value}" if value is not None and value != "" else None


def _calculate(config: dict, count: int, cap: float) -> tuple[float, str | None]:
    method = config.get("method", "manual")
    if method == "count":
        if "points_per_item" not in config or _number(config["points_per_item"], -1) < 0:
            return 0, "按项计分缺少有效的每项分值"
        points = count * _number(config["points_per_item"])
    elif method == "fixed":
        if "points" not in config or _number(config["points"], -1) < 0:
            return 0, "固定计分缺少有效分值"
        points = _number(config["points"]) if count else 0
    elif method == "tiered":
        tiers = config.get("tiers", [])
        if not tiers or any("min_count" not in item or "points" not in item or _number(item["min_count"], -1) < 0 or _number(item["points"], -1) < 0 for item in tiers):
            return 0, "分档计分缺少完整档位"
        points = max((_number(item["points"]) for item in tiers if count >= _number(item["min_count"])), default=0)
    else:
        return 0, "需按评分原文完成语义核验及确定性计分配置，响应自报分值不直接计入"
    return round(min(cap, max(0, points)), 4) if count else 0, None


def _independent(review: dict, section: dict) -> bool:
    return bool(section.get("writer_id") and review.get("reviewer_id") and review.get("writer_id") == section["writer_id"] and review["reviewer_id"] != section["writer_id"])


def _score(ctx: dict, selected_staff: set[str] | None = None) -> dict:
    matching = _match(ctx, selected_staff)
    checks = {row["rule_id"]: row for row in matching["rules"]}
    rows = []
    for rule in ctx["rules"]:
        if rule.get("status") == "retired" or rule.get("kind") != "business":
            continue
        check = checks[rule["id"]]
        config = _scoring(rule)
        maximum = max(0, _number(rule.get("max_score")))
        cap = min(maximum, max(0, _number(config.get("cap"), maximum)))
        problems = list(check["problems"])
        keys, potential_keys = set(), set()
        key_evidence = {}
        used_evidence = []
        candidate_count = 0
        for response in check["responses"]:
            for evidence_check in response["evidence"]:
                evidence = ctx["evidence_by_id"].get(evidence_check["id"])
                if not evidence:
                    continue
                key = _item_key(evidence, config)
                if key is None:
                    problems.append(f"证据 {evidence['id']} 缺少去重字段 {config.get('distinct_by', 'file_id')}")
                    continue
                if evidence.get("verification") != "rejected":
                    potential_keys.add(key)
                if check["supported"] and response["valid"] and evidence_check["valid"]:
                    keys.add(key)
                    used_evidence.append(evidence["id"])
                    key_evidence.setdefault(key, []).append(evidence["id"])
            if config.get("method") == "fixed" and response["valid"] and not response["evidence"] and check["supported"]:
                # 固定分声明允许规则显式采用正文证明，不能据此虚构多项业绩。
                keys.add("confirmed_section")
                potential_keys.add("confirmed_section")
        rule_candidates = [item for item in matching["candidates"] if item["rule_id"] == rule["id"]]
        candidate_count = len({item["project_id"] for item in rule_candidates})
        supported, note = _calculate(config, len(keys), cap)
        if note:
            problems.append(note)
        potential, _ = _calculate(config, max(len(potential_keys), candidate_count), cap)
        if _check_rule(ctx, rule):
            potential = 0
        rows.append({"rule_id": rule["id"], "title": rule.get("title", ""), "max_score": maximum, "supported_score": supported, "potential_additional_score": round(max(0, potential - supported), 4), "evidence_ids": list(dict.fromkeys(used_evidence)), "distinct_keys": sorted(keys), "potential_keys": sorted(potential_keys), "key_evidence": key_evidence, "problems": list(dict.fromkeys(problems)), "scoring": config})
    # 共享材料去重组：稳定按规则ID分配，同组后项不能再使用前项已用材料。
    seen_groups: dict[str, set[str]] = {}
    potential_seen: dict[str, set[str]] = {}
    for row in sorted(rows, key=lambda item: item["rule_id"]):
        config = row["scoring"]
        group = config.get("dedupe_group")
        if group:
            seen = seen_groups.setdefault(group, set())
            keys = set(row["distinct_keys"]) - seen
            if len(keys) != len(row["distinct_keys"]):
                row["problems"].append("同一去重组内的材料已分配给编号在前的评分项，未重复计分")
            seen.update(keys)
            potential_used = potential_seen.setdefault(group, set())
            potential_keys = set(row["potential_keys"]) - potential_used
            potential_used.update(potential_keys)
            cap = min(row["max_score"], _number(config.get("cap"), row["max_score"]))
            row["supported_score"], _ = _calculate(config, len(keys), cap)
            potential_value, _ = _calculate(config, len(potential_keys), cap)
            row["potential_additional_score"] = max(0, round(potential_value - row["supported_score"], 4))
            row["distinct_keys"] = sorted(keys)
            row["evidence_ids"] = list(dict.fromkeys(eid for key in sorted(keys) for eid in row["key_evidence"].get(key, [])))
    # 互斥组只保留支持分最高的一项；支持分相同按编号确定，便于重复执行。
    exclusive: dict[str, list[dict]] = {}
    for row in rows:
        if row["scoring"].get("exclusive_group"):
            exclusive.setdefault(row["scoring"]["exclusive_group"], []).append(row)
    for group_rows in exclusive.values():
        winner = sorted(group_rows, key=lambda row: (-row["supported_score"], row["rule_id"]))[0]
        group_potential = max(row["supported_score"] + row["potential_additional_score"] for row in group_rows)
        for row in group_rows:
            if row is not winner:
                row["supported_score"] = 0
                row["potential_additional_score"] = 0
                row["evidence_ids"] = []
                row["problems"].append(f"与 {winner['rule_id']} 属于互斥评分项，本次未叠加")
        winner["potential_additional_score"] = max(0, round(group_potential - winner["supported_score"], 4))
    simulated = []
    for review in ctx["reviews"]:
        section = ctx["sections_by_id"].get(review.get("section_id"))
        if not section or review.get("section_sha256") != section.get("sha256") or _check_section(ctx, section["id"]) or not _independent(review, section):
            continue
        if review.get("status") not in ("completed", "confirmed", "accepted", "pass", "revise"):
            continue
        for item in review.get("scores", []):
            rule = next((r for r in ctx["rules"] if r["id"] == item.get("rule_id") and r.get("kind") == "technical" and r.get("status") != "retired"), None)
            if rule:
                simulated.append({"rule_id": rule["id"], "section_id": section["id"], "review_id": review["id"], "score": min(max(0, _number(item.get("score"))), _number(rule.get("max_score"))), "reason": item.get("reason", ""), "label": "独立复核模拟分，不能视为评委得分"})
    price = [{"rule_id": rule["id"], "max_score": rule.get("max_score"), "status": "pending", "reason": "报价评分单列，未获完整评标公式及所需报价数据时不计入商务证据分"} for rule in ctx["rules"] if rule.get("kind") == "price" and rule.get("status") != "retired"]
    return {"status": "estimate", "summary": "商务证据测算、补证空间、技术模拟及报价评分分别列示，均不保证最终得分。", "business_supported_score": round(sum(row["supported_score"] for row in rows), 4), "business_potential_additional_score": round(sum(row["potential_additional_score"] for row in rows), 4), "business": rows, "technical_simulations": simulated, "price": price, "missing_count": len(matching["missing"]), "allocation_policy": "去重组按规则编号顺序分配；互斥组保留现有证据支持分最高项"}


def score(project) -> dict:
    """按显式计分配置计算，不采用模型自报分值替代证据核验。"""
    return _score(_context(project))


def _meets(value: Any, requirement: dict) -> bool:
    operator = requirement.get("op", "eq")
    expected = requirement.get("value")
    if operator == "eq":
        return value == expected
    if operator == "in":
        return value in expected if isinstance(expected, list) else False
    if operator in ("min", "max"):
        try:
            left, right = Decimal(str(value)), Decimal(str(expected))
            return left.is_finite() and right.is_finite() and (left >= right if operator == "min" else left <= right)
        except (InvalidOperation, TypeError):
            return False
    raise ValueError(f"不支持的人员约束操作：{operator}")


def optimize_staff(project, config: dict) -> dict:
    """在明确候选和约束范围内枚举，证据分优先；截断时不宣称最优。"""
    ctx = _context(project)
    positions = config.get("positions", [])
    if not positions or len({row.get("id") for row in positions}) != len(positions) or any(not row.get("id") for row in positions):
        raise ValueError("人员配置需要非空且岗位ID不重复的 positions")
    limit = int(config.get("max_combinations", 10000))
    if limit < 1 or limit > 1000000:
        raise ValueError("枚举上限应在 1 至 1000000 之间")
    staff_by_id = {row["id"]: row for row in ctx["staff"]}
    rules_by_id = {row["id"]: row for row in ctx["rules"]}
    mapped_rules = {rule_id for position in positions for rule_id in position.get("eligibility_rule_ids", []) + position.get("scoring_rule_ids", [])}
    if len(positions) > 1:
        for rule in ctx["rules"]:
            personnel_responses = [row for row in ctx["responses"] if row.get("rule_id") == rule["id"] and row.get("staff_id")]
            if rule.get("kind") == "business" and rule.get("status") != "retired" and personnel_responses and rule["id"] not in mapped_rules and not rule.get("criteria", {}).get("personnel", {}).get("position_id") and any(not row.get("position_id") for row in personnel_responses):
                raise ValueError(f"人员评分项 {rule['id']} 尚未绑定岗位，请在 scoring_rule_ids 或 position_id 中明确，避免跨岗位误计分")
    candidate_lists, exclusions = [], []
    for position in positions:
        acceptable = []
        for rule_id in position.get("scoring_rule_ids", []):
            if rule_id not in rules_by_id or rules_by_id[rule_id].get("kind") != "business":
                raise ValueError(f"岗位 {position['id']} 引用了不存在或非商务的评分规则 {rule_id}")
        candidate_ids = list(dict.fromkeys(position.get("candidate_ids", position.get("candidates", list(staff_by_id)))))
        for staff_id in candidate_ids:
            person = staff_by_id.get(staff_id)
            if person is None:
                raise ValueError(f"岗位 {position['id']} 引用了不存在的人员 {staff_id}")
            reasons = []
            if person.get("available") is False:
                reasons.append("人员已确认不可用")
            if config.get("require_available") and person.get("available") is not True:
                reasons.append("岗位要求事先确认人员可用性")
            if not person.get("source_rows"):
                reasons.append("人员缺少台账来源定位")
            for rule_id in position.get("eligibility_rule_ids", []):
                rule = rules_by_id.get(rule_id)
                if not rule:
                    raise ValueError(f"岗位引用了不存在的资格规则 {rule_id}")
                owned = [row for row in ctx["responses"] if row.get("rule_id") == rule_id and row.get("staff_id") == staff_id]
                local = dict(ctx, responses=owned)
                if not _evaluate(local, rule, {staff_id})["supported"]:
                    reasons.append(f"尚未证明符合岗位资格规则 {rule_id}")
            evidence_facts = []
            for evidence in ctx["evidence"]:
                facts = evidence.get("facts", {})
                belongs = facts.get("staff_id") == staff_id or any(role.get("staff_id") == staff_id for role in facts.get("person_roles", []))
                if belongs and not _check_evidence(ctx, evidence):
                    evidence_facts.append(facts)
            for requirement in position.get("requires", []):
                field = requirement.get("fact")
                if not field or not any(field in facts and _meets(facts[field], requirement) for facts in evidence_facts):
                    reasons.append(f"缺少证据支持岗位硬条件：{field}")
            if reasons:
                exclusions.append({"position_id": position["id"], "staff_id": staff_id, "reasons": reasons})
            else:
                acceptable.append(staff_id)
        candidate_lists.append(acceptable)
    theoretical = math.prod(len(values) for values in candidate_lists)
    examined = 0
    feasible = 0
    recommendations = []
    incompatible = [set(pair) for pair in config.get("incompatible_staff", [])]
    allowed_dual = {frozenset(pair) for pair in config.get("allowed_dual_positions", [])}
    for combination in itertools.islice(itertools.product(*candidate_lists), limit):
        examined += 1
        valid = True
        for left in range(len(combination)):
            for right in range(left + 1, len(combination)):
                if combination[left] == combination[right] and not config.get("allow_dual_roles", False) and frozenset((positions[left]["id"], positions[right]["id"])) not in allowed_dual:
                    valid = False
        selected = set(combination)
        if not valid or any(pair <= selected for pair in incompatible):
            continue
        feasible += 1
        assignments = {position["id"]: staff_id for position, staff_id in zip(positions, combination)}
        owners = {}
        for position in positions:
            for rule_id in position.get("eligibility_rule_ids", []) + position.get("scoring_rule_ids", []):
                owners.setdefault(rule_id, set()).add(assignments[position["id"]])
        filtered = []
        for response in ctx["responses"]:
            staff_id = response.get("staff_id")
            if staff_id and staff_id not in selected:
                continue
            rule = rules_by_id.get(response.get("rule_id"), {})
            position_id = response.get("position_id") or rule.get("criteria", {}).get("personnel", {}).get("position_id")
            if position_id and (position_id not in assignments or staff_id != assignments[position_id]):
                continue
            if response.get("rule_id") in owners and staff_id not in owners[response["rule_id"]]:
                continue
            filtered.append(response)
        result = _score(dict(ctx, responses=filtered), selected)
        awaiting = [staff_id for staff_id in selected if staff_by_id[staff_id].get("available") is None]
        recommendation = {"assignments": [{"position_id": position["id"], "position_title": position.get("title", position["id"]), "staff_id": staff_id, "name": staff_by_id[staff_id].get("name"), "availability": "待确认" if staff_by_id[staff_id].get("available") is None else "已确认可用"} for position, staff_id in zip(positions, combination)], "supported_score": result["business_supported_score"], "potential_additional_score": result["business_potential_additional_score"], "missing_count": result["missing_count"], "availability_pending": awaiting, "scoring": result["business"]}
        recommendations.append(recommendation)
        recommendations.sort(key=lambda item: (-item["supported_score"], item["missing_count"], len(item["availability_pending"]), tuple(row["staff_id"] for row in item["assignments"])))
        del recommendations[3:]
    truncated = theoretical > examined
    result = {"id": "STAFFSOL-" + json_hash(config)[:12], "status": "partial" if truncated else "complete", "summary": "已达到枚举上限，以下仅为已检查范围内的候选方案，未宣称最优。" if truncated else "已枚举配置范围内的组合；方案仅依据当前已核验证据及显式约束。", "candidate_product": theoretical, "examined": examined, "feasible_count": feasible, "truncated": truncated, "recommendations": recommendations, "excluded": exclusions, "config": config, "generated_at": utc_now(), "input_fingerprint": project.fingerprint(["rules", "responses", "evidence", "staff", "history", "facts"])}
    saved = [row for row in project.load("staff_solutions", []) if row.get("id") != result["id"]] + [result]
    project.save("staff_solutions", saved, reason="生成可追溯的人员岗位组合建议")
    lines = ["# 人员配置建议", "", result["summary"], "", f"组合总数：{theoretical}；已检查：{examined}；可行：{feasible}。", ""]
    for number, item in enumerate(recommendations, 1):
        lines += [f"## 方案 {number}", "", f"当前证据支持商务分：{item['supported_score']}；补证后可能增加：{item['potential_additional_score']}；缺口数：{item['missing_count']}。", ""]
        lines += [f"- {row['position_title']}：{row['name']}（{row['staff_id']}，{row['availability']}）" for row in item["assignments"]]
        lines.append("")
    path = project.safe_path("03_资料匹配/人员配置建议.md")
    write_text(path, "\n".join(lines))
    result["report_path"] = path.relative_to(project.root).as_posix()
    return result


def audit(project) -> dict:
    """综合核查覆盖、版本、正文、表单和人工待办，不自动认定可提交。"""
    ctx = _context(project)
    matching = _match(ctx)
    issues = []
    def add(severity: str, category: str, message: str, refs: list[str] | None = None):
        issues.append({"id": f"AUD{len(issues) + 1:04d}", "severity": severity, "category": category, "message": message, "refs": refs or [], "status": "open"})
    if not any(rule.get("status") != "retired" for rule in ctx["rules"]):
        add("error", "rules", "尚无招标拆解规则，不能完成覆盖审核")
    if ctx["settings"].get("anonymous"):
        add("error", "scope", "存在暗标要求，超出首版支持范围")
    for row in matching["rules"]:
        if not row["supported"]:
            severity = "error" if row["kind"] in ("qualification", "invalid", "requirement") else "warning"
            add(severity, "coverage", f"{row['rule_id']} {row['title']}：{'；'.join(row['problems'])}", [row["rule_id"]])
        for response in row["responses"]:
            for evidence in response["evidence"]:
                if evidence["problems"]:
                    add("warning", "evidence", f"{evidence['id']}：{'；'.join(evidence['problems'])}", [row["rule_id"], evidence["id"]])
    for response in ctx["responses"]:
        if not any(rule["id"] == response.get("rule_id") and rule.get("status") != "retired" for rule in ctx["rules"]):
            add("warning", "stale", "响应指向不存在或已废止规则", [response.get("id", "")])
    max_rounds = int(ctx["settings"].get("max_review_rounds", 2))
    for section in ctx["sections"]:
        problems = _check_section(ctx, section["id"])
        if problems:
            add("warning", "section", "；".join(problems), [section["id"]])
        try:
            content = project.safe_path(section.get("path", "")).read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        if re.search(r"\{\{[^}]+\}\}|\b(?:TODO|TBD|XXX)\b|【(?:待填|待确认|待补)[^】]*】|(?<!_)_{3,}(?!_)", content, re.I):
            add("warning", "placeholder", "正文仍有待填占位内容", [section["id"]])
        section_rule_ids = set(section.get("rule_ids", [])) | {response["rule_id"] for response in ctx["responses"] if section["id"] in response.get("section_ids", [])}
        technical_rules = [rule for rule in ctx["rules"] if rule.get("kind") == "technical" and rule.get("status") != "retired" and rule["id"] in section_rule_ids]
        if not technical_rules:
            continue
        current_revisions = {rule["id"]: rule.get("revision", 1) for rule in technical_rules}
        current_facts = project.fingerprint(["facts"])
        current_reviews = [
            review for review in ctx["reviews"]
            if review.get("section_id") == section["id"]
            and review.get("section_sha256") == section.get("sha256")
            and review.get("rule_revisions") == current_revisions
            and review.get("facts_fingerprint") == current_facts
            and not problems
            and review.get("status") in ("completed", "confirmed", "accepted", "pass", "revise")
            and _independent(review, section)
        ]
        if not current_reviews:
            add("warning", "review", "当前正文版本尚无有效的独立复核", [section["id"]])
        else:
            review = max(current_reviews, key=lambda item: item.get("round", 0))
            if review.get("status") == "revise":
                add("warning", "review", "独立复核要求修改，当前正文尚未复核通过", [section["id"]])
            scored = {item.get("rule_id") for item in review.get("scores", []) if item.get("reason") and _number(item.get("score"), -1) >= 0}
            required_technical = {rule["id"] for rule in technical_rules}
            if required_technical - scored:
                add("warning", "review", "独立复核尚未逐项评估本章技术评分要求", [section["id"], *sorted(required_technical - scored)])
            for rule in technical_rules:
                assigned = {sub for response in ctx["responses"] if response.get("rule_id") == rule["id"] and section["id"] in response.get("section_ids", []) for sub in response.get("covered_subrequirements", [])}
                required_subs = assigned or set(rule.get("subrequirements", []))
                reviewed_subs = set(review.get("covered_subrequirements", {}).get(rule["id"], []))
                if required_subs - reviewed_subs:
                    add("warning", "review", "独立复核尚未确认本章声明响应的技术子要求：" + "、".join(sorted(required_subs - reviewed_subs)), [section["id"], rule["id"]])
            for finding in review.get("findings", []):
                if finding.get("status") != "resolved":
                    add("warning", "review", finding.get("message", "独立复核意见尚未处理"), [section["id"], finding.get("rule_id", "")])
        if any(review.get("round", 0) > max_rounds for review in ctx["reviews"] if review.get("section_id") == section["id"]):
            add("warning", "review", "复核轮次超过设置上限，需明确处理未解决问题", [section["id"]])
    shared = ctx["facts"]
    for form in ctx["forms"]:
        for problem in _check_form(ctx, form["id"]):
            add("warning", "form", problem, [form["id"]])
        values = {**shared, **form.get("fields", {})}
        for field in form.get("required_fields", []):
            if values.get(field) is None or str(values.get(field, "")).strip() == "":
                add("error", "form", f"表单必填字段 {field} 缺失", [form["id"]])
        for field in ("project_name", "company_name", "deadline", "service_period", "price", "price_upper", "price_uppercase", "staff_names"):
            equal = _number(values.get(field), -1) == _number(shared.get(field), -2) if field == "price" else str(values.get(field)).strip() == str(shared.get(field)).strip()
            if field in values and field in shared and not equal:
                add("error", "consistency", f"表单 {field} 与统一字段来源不一致", [form["id"]])
        if any(re.search(r"\{\{[^}]+\}\}|待填|待确认|TODO|TBD", str(value), re.I) for value in values.values()):
            add("warning", "placeholder", "表单字段仍含待填内容", [form["id"]])
        if not form.get("source_refs"):
            add("warning", "form", "表单未建立招标模板来源定位", [form["id"]])
        uppercase = values.get("price_upper", values.get("price_uppercase"))
        if uppercase is not None and values.get("price") is not None:
            from .forms import price_upper
            try:
                if str(uppercase).replace("人民币", "").replace(" ", "").replace("圆", "元").replace("正", "整") != price_upper(values["price"]):
                    add("error", "consistency", "表单报价大小写金额不一致", [form["id"]])
            except ValueError as exc:
                add("error", "consistency", str(exc), [form["id"]])
    if shared.get("deadline") and not _date(shared["deadline"]):
        add("error", "consistency", "投标截止日期格式无法核验")
    if "price" in shared and _number(shared["price"], -1) < 0:
        add("error", "consistency", "统一报价字段不是有效的非负数")
    for key in ("company_name", "deadline", "service_period", "price"):
        if key not in shared or shared[key] in (None, ""):
            add("warning", "facts", f"共用字段 {key} 尚未确认")
    # 对开放式文字只报告字段核对边界，不用姓名或日期关键词推断全文一致。
    checks = ctx["manual_checks"]
    for check_id, required, aliases in (("MC001", "签章复核", ("签字盖章及授权有效性", "签章复核")), ("MC002", "递交要求复核", ("投标保证金及提交手续", "递交要求复核")), ("MC003", "全文名称日期金额复核", ("全文名称日期金额复核",))):
        if not any(item.get("id") == check_id or item.get("title") in aliases for item in checks):
            add("warning", "manual", f"未建立必要人工检查项：{required}")
    for item in checks:
        if _check_manual(ctx, item["id"]):
            add("warning", "manual", item.get("title", "人工检查项") + "尚未通过", [item.get("id", "")])
    scoring_rules = [rule for rule in ctx["rules"] if rule.get("kind") in ("business", "technical", "price") and rule.get("status") != "retired"]
    expected = ctx["settings"].get("expected_total_score")
    if expected is not None and not math.isclose(sum(_number(rule.get("max_score")) for rule in scoring_rules), _number(expected), abs_tol=0.0001):
        add("error", "score", "商务、技术及报价评分满分合计与确认总分不一致")
    scoring = _score(ctx)
    for row in scoring["business"]:
        if row["problems"]:
            add("warning", "score", f"{row['rule_id']} 计分待核：" + "；".join(row["problems"]), [row["rule_id"]])
    if not ctx["assembly"] or not ctx["assembly"].get("outputs"):
        add("warning", "assembly", "尚未完成组卷、分页及输出链接复核")
    else:
        from .assembly import verify_output
        try:
            verification = verify_output(project)
            for problem in verification.get("errors", []):
                add("warning", "assembly", problem)
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            add("warning", "assembly", f"组卷输出无法核验：{exc}")
    return {"status": "blocked" if any(issue["severity"] == "error" for issue in issues) else "review_required" if issues else "ready_for_manual_submission_review", "summary": f"发现 {sum(item['severity'] == 'error' for item in issues)} 项阻断问题、{sum(item['severity'] == 'warning' for item in issues)} 项待复核事项；真实项目验收状态：{project.meta.get('acceptance_status', '待实标验收')}。", "issues": issues, "matching": matching, "score": scoring, "automatic_submission": False}


def _cell(value: Any) -> str:
    if isinstance(value, list):
        value = "；".join(str(item) for item in value)
    return str(value if value is not None else "待确认").replace("|", "\\|").replace("\n", "<br>")


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    return "\n".join(["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |", *["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows]]) + "\n"


def render_reports(project) -> dict:
    """生成可重建的 Markdown 报告，不改变主记录和正文。"""
    result = audit(project)
    matching, scoring = result["matching"], result["score"]
    paths = []
    def output(relative: str, title: str, body: str):
        path = project.safe_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text(path, f"# {title}\n\n本文件由当前主记录生成，可重建。台账候选、材料事实与本次规则适用性分别核验。\n\n{body}\n")
        paths.append(str(path))
    source_display = lambda row: [f"{item.get('file_id', '')} / {item.get('block_id', '')} / 页{item.get('page') or '待定位'}" for item in row["sources"]]
    spec_path = project.safe_path("02_招标拆解/bid_spec.json")
    atomic_json(spec_path, {
        "generated_at": utc_now(),
        "source": ".bidflow/records中的当前主记录，可由主记录重建",
        "project": project.meta,
        "facts": project.load("facts", {}),
        "settings": project.load("settings", {}),
        "rules": project.load("rules", []),
        "analysis_coverage": project.load("analysis_coverage", []),
    })
    paths.append(str(spec_path))
    for kind, filename, title in (("qualification", "资格审查表.md", "资格审查表"), ("invalid", "否决风险表.md", "否决投标风险表"), ("requirement", "必响应要求表.md", "必响应要求表")):
        output("02_招标拆解/" + filename, title, _table(["编号", "要求", "来源", "当前响应", "缺口"], [[row["rule_id"], row["title"], source_display(row), "已有有效响应" if row["supported"] else "待核验", row["problems"]] for row in matching["rules"] if row["kind"] == kind]))
    output("02_招标拆解/评分规则表.md", "商务、技术和报价评分规则表", _table(["编号", "类别", "要求", "来源", "覆盖缺口"], [[row["rule_id"], row["kind"], row["title"], source_display(row), row["problems"]] for row in matching["rules"] if row["kind"] in ("business", "technical", "price")]))
    match_rows = []
    for row in matching["rules"]:
        references = [ref for response in row["responses"] for ref in response["evidence"] + response["sections"] + response["forms"] + response["manual_checks"]]
        match_rows.append([row["rule_id"], "已有有效响应" if row["supported"] else "待核验", [ref["id"] for ref in references], [note for ref in references for note in ref["problems"]]])
    output("03_资料匹配/资料匹配表.md", "资料匹配表", _table(["要求", "响应状态", "材料、正文、表单及检查", "证明核验情况"], match_rows))
    output("03_资料匹配/商务评分测算.md", "商务评分测算", f"现有证据支持测算分：{scoring['business_supported_score']}；补证后可能增加：{scoring['business_potential_additional_score']}。\n\n{scoring['summary']}\n\n" + _table(["评分项", "满分", "证据支持分", "补证可能增加", "证据", "待核事项"], [[row["rule_id"], row["max_score"], row["supported_score"], row["potential_additional_score"], row["evidence_ids"], row["problems"]] for row in scoring["business"]]))
    borrow_rows = []
    for missing in matching["missing"]:
        suggested = {"section": "编写并确认对应正文，逐项回应要求", "form": "按招标模板填写表单并完成模板及字段确认", "manual": "完成关联人工检查，记录确认人及适用版本", "either": "按本项要求补充证据、承诺正文或招标表单", "both": "同时补齐原始证明材料及确认正文或表单"}.get(missing["proof_mode"], "原始合同、招标要求的补充证明及证明页；人员业绩需明确本人角色")
        for candidate in missing["candidates"] or [{}]:
            borrow_rows.append([missing["rule_id"], candidate.get("project_name", "本项响应" if missing["proof_mode"] != "evidence" else "待查找"), candidate.get("contract_no", "—" if missing["proof_mode"] != "evidence" else "待确认"), [f"{role.get('name') or role.get('staff_id')}：{role.get('role', '角色待确认')}" for role in candidate.get("roles", [])], missing["needed"], suggested])
    output("03_资料匹配/待补资料及借阅清单.md", "待补资料及借阅清单", _table(["要求", "项目名称", "合同编号", "台账角色", "待证明事项", "建议借阅内容"], borrow_rows))
    matrix = _table(["评分项", "章节", "未覆盖子要求", "当前状态"], [[row["rule_id"], [item["id"] for response in row["responses"] for item in response["sections"]], row["missing_subrequirements"], row["problems"] or "当前子要求已响应，仍需独立复核"] for row in matching["rules"] if row["kind"] == "technical"])
    output("04_技术标策划/技术评分响应矩阵.md", "技术评分响应矩阵", matrix)
    plans = project.load("plans", [])
    plan_body = _table(
        ["章节", "对应要求", "计划内容", "项目事实", "方法论", "图表", "确认状态"],
        [[row.get("title", row["id"]), row.get("rule_ids", []), row.get("content_points", []), row.get("fact_refs", []), row.get("method_refs", []), row.get("visuals", []), row.get("status", "draft")] for row in plans],
    ) if plans else "技术标策划尚未形成。"
    output("04_技术标策划/技术标策划.md", "技术标策划", plan_body)
    output("06_审核检查/技术评分覆盖复核.md", "技术评分覆盖复核", matrix)
    output("06_审核检查/审核清单.md", "审核清单", result["summary"] + "\n\n" + _table(["编号", "等级", "类别", "问题", "关联项"], [[item["id"], item["severity"], item["category"], item["message"], item["refs"]] for item in result["issues"]]))
    output("06_审核检查/技术模拟评分.md", "技术模拟评分", "此表只展示独立复核的模拟分，不合并为商务证据分。\n\n" + _table(["评分项", "章节", "复核记录", "模拟分", "依据"], [[row["rule_id"], row["section_id"], row["review_id"], row["score"], row["reason"]] for row in scoring["technical_simulations"]]))
    return {"status": result["status"], "paths": paths, "summary": "已重建资格、否决、评分、证据匹配、缺件及审核报告。"}
