"""保留模板格式的商务表单填充；确认由用户完成。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import re
from zipfile import ZipFile, ZIP_DEFLATED

from lxml import etree

from .utils import sha256_file, utc_now, write_text

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_\u4e00-\u9fff][\w.\u4e00-\u9fff]*)\s*\}\}")
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def price_upper(value) -> str:
    """人民币金额转大写，用于核对用户已经确定的报价。"""
    try:
        amount = Decimal(str(value).replace(",", "").replace("￥", "").replace("¥", "")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("报价必须是有效数字") from exc
    if not amount.is_finite() or amount < 0 or amount >= Decimal("10000000000000000"):
        raise ValueError("报价必须为非负数且小于一亿亿元")
    digits = "零壹贰叁肆伍陆柒捌玖"
    integer, fraction = divmod(int(amount * 100), 100)

    def group(number):
        text, zero = "", False
        for power, unit in [(3, "仟"), (2, "佰"), (1, "拾"), (0, "")]:
            digit = number // (10 ** power) % 10
            if digit:
                if zero and text:
                    text += "零"
                text += digits[digit] + unit
                zero = False
            elif text:
                zero = True
        return text

    result, pending_zero = "", False
    for power, unit in [(3, "万亿"), (2, "亿"), (1, "万"), (0, "")]:
        number = integer // (10000 ** power) % 10000
        if number:
            if result and (pending_zero or number < 1000):
                result += "零"
            result += group(number) + unit
            pending_zero = False
        elif result:
            pending_zero = True
    result = (result or "零") + "元"
    jiao, fen = divmod(fraction, 10)
    if not fraction:
        return result + "整"
    if jiao:
        result += digits[jiao] + "角"
    elif integer and fen:
        result += "零"
    if fen:
        result += digits[fen] + "分"
    return result


def _replace_xml(data: bytes, values: dict) -> tuple[bytes, set[str]]:
    """跨文本 run 替换占位符，保留未变文字及首个 run 的原有格式。"""
    root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))
    found = set()
    for paragraph in root.iter(W + "p"):
        nodes = list(paragraph.iter(W + "t"))
        combined = "".join(node.text or "" for node in nodes)
        matches = list(PLACEHOLDER.finditer(combined))
        found.update(match.group(1) for match in matches)
        for match in reversed(matches):
            key = match.group(1)
            if key not in values or values[key] is None or str(values[key]).strip() == "":
                continue
            cursor, started = 0, False
            for node in nodes:
                old = node.text or ""
                start, end = cursor, cursor + len(old)
                cursor = end
                if end <= match.start() or start >= match.end():
                    continue
                left = old[:max(0, match.start() - start)]
                right = old[max(0, match.end() - start):] if match.end() <= end else ""
                node.text = left + (str(values[key]) if not started else "") + right
                node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                started = True
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True), found


def fill_form(project, form_id) -> dict:
    forms = project.load("forms", [])
    form = next((item for item in forms if item["id"] == form_id), None)
    if not form:
        raise ValueError(f"未找到表单：{form_id}")
    source = project.safe_path(form["template_path"])
    if not source.is_file() or source.suffix.lower() not in {".docx", ".md"}:
        raise ValueError("表单模板必须为项目内已有的 DOCX 或 Markdown 文件")
    facts = project.load("facts", {})
    values = {**facts, **form.get("fields", {})}
    stamp = utc_now().replace(":", "").replace("-", "").replace(".", "")
    folder = project.safe_path("05_投标文件编制/商务表单")
    folder.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^\w-]", "_", str(form_id))
    output = folder / f"{safe_id}_{stamp}{source.suffix.lower()}"
    if output.exists():
        from uuid import uuid4
        output = output.with_name(f"{output.stem}_{uuid4().hex[:6]}{output.suffix}")
    source_hash = sha256_file(source)
    found = set()
    if source.suffix.lower() == ".docx":
        with ZipFile(source) as archive, ZipFile(output, "w", ZIP_DEFLATED) as destination:
            for item in archive.infolist():
                data = archive.read(item.filename)
                if item.filename.startswith("word/") and item.filename.endswith(".xml"):
                    data, fields = _replace_xml(data, values)
                    found.update(fields)
                destination.writestr(item, data)
    else:
        text = source.read_text(encoding="utf-8-sig")
        found = {match.group(1) for match in PLACEHOLDER.finditer(text)}
        text = PLACEHOLDER.sub(lambda match: str(values[match.group(1)]) if values.get(match.group(1)) is not None and str(values[match.group(1)]).strip() else match.group(0), text)
        write_text(output, text)
    missing = sorted(key for key in found | set(form.get("required_fields", [])) if values.get(key) is None or not str(values[key]).strip())
    errors, warnings = [], []
    for key, value in form.get("fields", {}).items():
        if key in facts and facts[key] is not None and value is not None and str(value).strip() != str(facts[key]).strip():
            errors.append(f"字段 {key} 与项目共用事实不一致，请修正表单字段后重新填充")
    if missing:
        errors.append("缺少字段：" + "、".join(missing))
    if not form.get("template_approved", False):
        warnings.append("招标模板尚未经用户核准，填充成功不代表格式满足招标要求")
    if form.get("template_sha256") and form["template_sha256"] != source_hash:
        form["template_approved"] = False
        warnings.append("模板内容发生变化，需重新核准模板")
    if values.get("price") is not None and values.get("price_upper"):
        try:
            expected = price_upper(values["price"])
            actual = str(values["price_upper"]).replace("人民币", "").replace(" ", "").replace("圆", "元").replace("正", "整")
            if actual != expected:
                errors.append(f"报价大小写不一致；按小写金额核对的大写为：{expected}")
        except ValueError as exc:
            errors.append(str(exc))
    if "price" in found and facts.get("price") is None:
        errors.append("项目共用事实尚无用户确定的报价；不得仅凭模板字段视为报价已确认")
    result = {"form_id": form_id, "output_path": output.relative_to(project.root).as_posix(), "template_sha256": source_hash, "output_sha256": sha256_file(output), "missing_fields": missing, "errors": errors, "warnings": warnings, "status": "draft", "signature_status": "pending"}
    form.update({key: value for key, value in result.items() if key != "form_id"})
    form["filled_at"] = utc_now()
    project.save("forms", forms, reason=f"填充表单 {form_id}，原模板保留，结果待确认")
    lines = [f"# {form.get('title', form_id)} 填充检查", "", f"输出：{result['output_path']}", "", "状态：待确认；签字盖章待人工办理。", "", *(f"- {message}" for message in errors + warnings)]
    report = output.with_suffix(".检查.md")
    write_text(report, "\n".join(lines) + "\n")
    result["report_path"] = report.relative_to(project.root).as_posix()
    return result
