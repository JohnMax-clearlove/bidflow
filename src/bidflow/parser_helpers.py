"""保留原文位置的解析器；不把识别失败解释成不存在要求。"""

from __future__ import annotations

import collections
import importlib.util
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree


def _block(text, kind="paragraph", page=None, section="", locator="", quality="ok", **extra):
    return {"text": text.strip(), "kind": kind, "page": page, "section": section,
            "locator": locator, "quality": quality, **extra}


def _suspicious(text: str) -> bool:
    return "\ufffd" in text or "(cid:" in text or any(ord(c) < 9 for c in text)


def _heading(text: str) -> bool:
    return bool(re.match(r"^(#{1,6}\s+|第[一二三四五六七八九十百\d]+[章节篇]|[一二三四五六七八九十]+、)", text.strip()))


def _parse_text(path: Path) -> dict:
    raw = path.read_bytes()
    warnings = []
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gb18030")
            warnings.append("原文按 GB18030 解码，请核对专有名词及特殊符号。")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
            warnings.append("文件编码无法可靠识别，已保留替换标记并列为人工核对。")
    blocks, buffer, start, section, in_code = [], [], 1, "", False

    def flush(end):
        nonlocal buffer
        if buffer:
            value = "\n".join(buffer)
            blocks.append(_block(value, "paragraph", section=section,
                                 locator=f"line:{start}-{end}", quality="review" if _suspicious(value) else "ok"))
            buffer = []

    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        if line.lstrip().startswith(("```", "~~~")):
            in_code = not in_code
        if not in_code and _heading(line):
            flush(number - 1)
            section = line.lstrip("# ").strip()
            blocks.append(_block(line, "heading", section=section, locator=f"line:{number}"))
        elif not line.strip() and not in_code:
            flush(number - 1)
        else:
            if not buffer:
                start = number
            buffer.append(line)
    flush(len(lines))
    if not blocks:
        blocks = [_block("", locator="line:1", quality="review")]
        warnings.append("未提取到正文，不能据此认定文件没有要求。")
    return {"blocks": blocks, "warnings": warnings, "page_count": None, "previews": []}


def _docx_body_elements(body):
    """展开内容控件，表内段落由完整表格统一处理，避免漏掉控件正文。"""
    for element in body:
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in {"p", "tbl", "altChunk"}:
            yield element
        elif tag != "sectPr":
            yield from _docx_body_elements(element)


def _render_docx(path: Path, cache: Path) -> dict:
    """在副本添加定位书签，Word 实际分页只用于来源位置，不覆盖原件。"""
    if os.name != "nt":
        raise RuntimeError("当前系统未提供 Microsoft Word 分页")
    from lxml import etree
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    executable = shutil.which("powershell") or shutil.which("pwsh")
    if not executable:
        raise RuntimeError("未找到 Word 分页所需的 PowerShell")
    source_copy, output_pdf = cache / "source-bookmarks.docx", cache / "source-rendered.pdf"
    with zipfile.ZipFile(path) as incoming:
        body_xml = etree.fromstring(incoming.read("word/document.xml"))
        body = body_xml.find(qn("w:body"))
        counters, positions = {"p": 0, "tbl": 0}, []
        used_ids = [int(e.get(qn("w:id"))) for e in body_xml.iter(qn("w:bookmarkStart")) if (e.get(qn("w:id")) or "").isdigit()]
        next_id = max(used_ids, default=0) + 1
        for element in _docx_body_elements(body):
            tag = element.tag.rsplit("}", 1)[-1]
            if tag not in counters:
                continue
            counters[tag] += 1
            locator = f"word/document.xml/{tag}[{counters[tag]}]"
            paragraphs = [element] if tag == "p" else list(element.iter(qn("w:p")))
            if not paragraphs:
                continue
            bookmark = f"BFSource_{next_id}"
            start, end = OxmlElement("w:bookmarkStart"), OxmlElement("w:bookmarkEnd")
            start.set(qn("w:id"), str(next_id))
            start.set(qn("w:name"), bookmark)
            end.set(qn("w:id"), str(next_id))
            paragraphs[0].insert(1 if len(paragraphs[0]) and paragraphs[0][0].tag == qn("w:pPr") else 0, start)
            paragraphs[-1].append(end)
            positions.append({"locator": locator, "bookmark": bookmark})
            next_id += 1
        with zipfile.ZipFile(source_copy, "w", zipfile.ZIP_DEFLATED) as outgoing:
            for member in incoming.infolist():
                outgoing.writestr(member, etree.tostring(body_xml, encoding="UTF-8", xml_declaration=True, standalone=True) if member.filename == "word/document.xml" else incoming.read(member.filename))
    request_path, result_path = cache / "word-source-request.json", cache / "word-source-result.json"
    request_path.write_text(json.dumps({"source": str(source_copy), "pdf": str(output_pdf), "positions": positions}, ensure_ascii=False), encoding="utf-8")
    # 路径通过环境变量传入，避免路径字符成为 PowerShell 代码。
    script = r'''$ErrorActionPreference = 'Stop'
$word = $null; $document = $null
try {
  $request = Get-Content -LiteralPath $env:BIDFLOW_SOURCE_REQUEST -Raw -Encoding UTF8 | ConvertFrom-Json
  $word = New-Object -ComObject Word.Application
  $word.Visible = $false
  $word.DisplayAlerts = 0
  $word.AutomationSecurity = 3
  $word.Options.UpdateLinksAtOpen = $false
  $document = $word.Documents.Open([string]$request.source, $false, $true, $false)
  $document.Repaginate()
  $positions = @()
  foreach ($item in $request.positions) {
    $range = $document.Bookmarks.Item([string]$item.bookmark).Range
    $first = $range.Duplicate; $first.Collapse(1)
    $last = $range.Duplicate
    if ($last.End -gt $last.Start) { $last.End = $last.End - 1 }
    $last.Collapse(0)
    $positions += @{ locator = $item.locator; page = [int]$first.Information(3); end_page = [int]$last.Information(3) }
  }
  $count = [int]$document.ComputeStatistics(2)
  $document.ExportAsFixedFormat([string]$request.pdf, 17)
  @{status='ok'; page_count=$count; positions=$positions} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $env:BIDFLOW_SOURCE_RESULT -Encoding UTF8
} catch {
  @{status='error'; error=$_.Exception.Message} | ConvertTo-Json | Set-Content -LiteralPath $env:BIDFLOW_SOURCE_RESULT -Encoding UTF8
} finally {
  if ($null -ne $document) { $document.Close(0); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($document) }
  if ($null -ne $word) { $word.Quit(); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($word) }
}'''
    env = {**os.environ, "BIDFLOW_SOURCE_REQUEST": str(request_path), "BIDFLOW_SOURCE_RESULT": str(result_path)}
    result_path.unlink(missing_ok=True)
    subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, timeout=180, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    if not result_path.exists():
        raise RuntimeError("Word 未返回分页结果")
    result = json.loads(result_path.read_text(encoding="utf-8-sig"))
    if result.get("status") != "ok" or not output_pdf.is_file():
        raise RuntimeError(result.get("error", "Word 未生成 PDF"))
    result["rendered_pdf"] = str(output_pdf)
    return result


def _parse_docx(path: Path, cache: Path, config: dict) -> dict:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(path)
    warnings, blocks, section = [], [], ""
    counters = {"p": 0, "tbl": 0}
    for element in _docx_body_elements(document.element.body):
        tag = element.tag.rsplit("}", 1)[-1]
        if tag not in counters:
            if tag == "altChunk":
                blocks.append(_block("", locator="word/document.xml/altChunk", quality="review"))
                warnings.append("DOCX 存在外部嵌入内容，需要人工核对。")
            continue
        counters[tag] += 1
        locator = f"word/document.xml/{tag}[{counters[tag]}]"
        flags = [name for name in ("ins", "del", "drawing", "object", "txbxContent") if element.xpath(f".//w:{name}")]
        if tag == "p":
            paragraph = Paragraph(element, document)
            # 保留超链接、插入删除修订及文本框的文字；含修订内容不得直接认定为最终原文。
            value = "".join(node.text or "" for node in element.iter() if node.tag in {qn("w:t"), qn("w:delText")})
            style = paragraph.style.name if paragraph.style else ""
            kind = "heading" if style.lower().startswith("heading") or _heading(value) else "paragraph"
            if kind == "heading":
                section = value
            if value or flags:
                blocks.append(_block(value, kind, section=section, locator=locator,
                                     quality="review" if flags or _suspicious(value) else "ok", source_flags=flags))
        else:
            table = Table(element, document)
            rows = []
            for row in table.rows:
                values = []
                for cell in row.cells:
                    values.append("".join(node.text or "" for node in cell._tc.iter() if node.tag in {qn("w:t"), qn("w:delText")}).replace("|", "\\|"))
                rows.append(values)
            value = "\n".join("| " + " | ".join(row) + " |" for row in rows)
            if len(rows) > 1:
                parts = value.splitlines()
                parts.insert(1, "| " + " | ".join("---" for _ in rows[0]) + " |")
                value = "\n".join(parts)
            complex_table = bool(element.xpath(".//w:vMerge | .//w:gridSpan | .//w:tbl/w:tr/w:tc/w:tbl"))
            blocks.append(_block(value, "table", section=section, locator=locator,
                                 quality="review" if flags or complex_table or _suspicious(value) else "ok",
                                 cells=rows, source_flags=flags))
            if complex_table:
                warnings.append(f"{locator} 含合并单元格或嵌套表格，需与原表核对。")
        if flags:
            warnings.append(f"{locator} 含修订、图像或嵌入对象，需要查看原文；DOCX 页码尚未渲染。")
    namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        parts = ["word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"]
        parts += [name for name in archive.namelist() if re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)]
        for part in parts:
            if part not in archive.namelist():
                continue
            root = ElementTree.fromstring(archive.read(part))
            for index, item in enumerate(root, 1):
                value = "".join(t.text or "" for t in item.findall(".//w:t", namespaces))
                if value.strip():
                    blocks.append(_block(value, locator=f"{part}/item[{index}]", quality="review"))
                    warnings.append(f"已提取 {part}，其适用性需要原文核对。")
    if not blocks:
        blocks.append(_block("", locator="word/document.xml", quality="review"))
        warnings.append("DOCX 未提取到正文；可能为图片文件，需要人工检查。")
    pandoc = shutil.which("pandoc")
    semantic = ""
    if pandoc:
        try:
            completed = subprocess.run([pandoc, str(path), "--from=docx", "--to=gfm", "--track-changes=all", "--wrap=none"],
                                       capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
            if completed.returncode == 0:
                semantic = completed.stdout
            else:
                warnings.append("Pandoc 转换失败，已保留 DOCX 结构定位文本，等待人工核对。")
        except (OSError, subprocess.TimeoutExpired):
            warnings.append("Pandoc 未能完成转换，已保留 DOCX 结构定位文本。")
    else:
        warnings.append("未检测到 Pandoc，已保留 DOCX 结构定位文本；补装后可重新解析。")
    result = {"blocks": blocks, "warnings": warnings, "page_count": None,
              "previews": [], "semantic_markdown": semantic, "page_basis": "unrendered"}
    if config.get("docx_render", True):
        try:
            rendered = _render_docx(path, cache)
            position_map = {item["locator"]: item for item in rendered["positions"]}
            for block in blocks:
                if block["locator"] in position_map:
                    block.update({key: position_map[block["locator"]][key] for key in ("page", "end_page")})
                    block["page_basis"] = "word_rendered"
            result.update({"page_count": rendered["page_count"], "page_basis": "word_rendered", "rendered_pdf": rendered["rendered_pdf"]})
        except Exception as error:
            warnings.append(f"Word 来源分页未完成（{type(error).__name__}），段落定位保留，页码不得视为已知。")
    else:
        warnings.append("DOCX 来源分页已关闭；段落和表格可定位，物理页码保持未知。")
    # 嵌入图像单独识别。即使附近有文字也不能省略图片里的资格或评分条件。
    for relationship in document.part.rels.values():
        if relationship.is_external or not relationship.reltype.endswith("/image"):
            continue
        part = relationship.target_part
        image_path = cache / "embedded" / (relationship.rId + Path(part.partname).suffix)
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(part.blob)
        image_result = parse_document(image_path, cache / "embedded" / relationship.rId, {**config, "docx_render": False})
        result["previews"].extend(image_result["previews"])
        for block in image_result["blocks"]:
            block.update({"page": None, "locator": f"word/media/{relationship.rId}/{block['locator']}", "quality": "review"})
            blocks.append(block)
        warnings.extend(image_result["warnings"])
    result["warnings"] = list(dict.fromkeys(warnings))
    return result


def _ocr(image: Path, config: dict) -> tuple[list[dict], str | None]:
    if not config["enabled"]:
        return [], "尚未开启本地 OCR，请查看原页或配置本地 OCR 后重试。"
    if importlib.util.find_spec("rapidocr") is None:
        return [], "未安装 RapidOCR，原页已保留，等待识别或人工核对。"
    models = config.get("model_paths", {})
    if not all(key in models and Path(models[key]).is_file() for key in ("det", "cls", "rec")):
        return [], "本地 OCR 模型不完整；需配置 det、cls、rec 模型路径，程序不会自动下载。"
    try:
        from rapidocr import RapidOCR
        params = {f"{key.title()}.model_path": str(Path(models[key]).resolve()) for key in ("det", "cls", "rec")}
        if models.get("keys"):
            if not Path(models["keys"]).is_file():
                return [], "OCR 字典文件不存在，等待补充本地字典。"
            params["Rec.rec_keys_path"] = str(Path(models["keys"]).resolve())
        engine = RapidOCR(params=params)
        result = engine(str(image))
        texts = getattr(result, "txts", None)
        boxes = getattr(result, "boxes", None)
        scores = getattr(result, "scores", None)
        if texts is None:
            return [], "OCR 未识别出正文，请人工核对原页。"
        lines = []
        for i, text in enumerate(texts):
            box = (boxes[i].tolist() if hasattr(boxes[i], "tolist") else boxes[i]) if boxes is not None else None
            confidence = float(scores[i]) if scores is not None else 0.0
            lines.append({"text": str(text), "ocr_box": box, "confidence": confidence})
        return lines, None
    except Exception as error:
        return [], f"本地 OCR 运行失败（{type(error).__name__}），请人工核对原页。"


def _render_pdf_page(path: Path, number: int, cache: Path, config: dict) -> Path:
    import pypdfium2
    preview = cache / "pages" / f"page-{number:04d}.png"
    preview.parent.mkdir(parents=True, exist_ok=True)
    pdf = pypdfium2.PdfDocument(path)
    try:
        page = pdf[number - 1]
        try:
            bitmap = page.render(scale=config["dpi"] / 72)
            try:
                image = bitmap.to_pil()
                try:
                    image.save(preview)
                finally:
                    image.close()
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        pdf.close()
    return preview


def _normalize_edge(text: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", "", text))


def _parse_pdf(path: Path, cache: Path, config: dict) -> dict:
    import pdfplumber
    blocks, warnings, previews = [], [], []
    section, edge_candidates = "", []
    try:
        pdf = pdfplumber.open(path)
    except Exception as error:
        return {"blocks": [_block("", locator="PDF文件", quality="review")], "warnings": [f"PDF 无法打开（{type(error).__name__}），可能加密或损坏，不能判定无要求。"], "page_count": None, "previews": []}
    with pdf:
        for number, page in enumerate(pdf.pages, 1):
            page_blocks, raw_text, errors = [], "", []
            try:
                raw_text = page.extract_text() or ""
                tables = page.find_tables()
                rectangles = [table.bbox for table in tables]
                for index, table in enumerate(tables, 1):
                    cells = table.extract()
                    text = "\n".join(" | ".join((cell or "").replace("\n", " ") for cell in row) for row in cells)
                    page_blocks.append(_block(text, "table", number, section, f"page:{number}/table:{index}", "review",
                                              bbox=list(table.bbox), cells=cells))
                lines = page.extract_text_lines()
                for index, line in enumerate(lines, 1):
                    # 表内文字已由表格块记录，避免重复计算内容覆盖。
                    center = ((line["x0"] + line["x1"]) / 2, (line["top"] + line["bottom"]) / 2)
                    if any(x0 <= center[0] <= x1 and y0 <= center[1] <= y1 for x0, y0, x1, y1 in rectangles):
                        continue
                    value = line["text"]
                    kind = "heading" if _heading(value) else "paragraph"
                    if kind == "heading":
                        section = value
                    block = _block(value, kind, number, section, f"page:{number}/line:{index}", "review" if _suspicious(value) else "ok",
                                   bbox=[line["x0"], line["top"], line["x1"], line["bottom"]])
                    page_blocks.append(block)
                    if line["top"] < page.height * .06 or line["bottom"] > page.height * .94:
                        edge_candidates.append(block)
            except Exception as error:
                errors.append(f"第 {number} 页文字或表格提取失败（{type(error).__name__}）。")
                if raw_text:
                    page_blocks.append(_block(raw_text, page=number, section=section, locator=f"page:{number}/fallback", quality="review"))
            image_area = sum(max(0, float(i.get("width", 0))) * max(0, float(i.get("height", 0))) for i in page.images)
            # 小图片也可能承载评分条件。不能仅凭整页已有文字或图片面积小就略过。
            likely_scan = len(re.sub(r"\s+", "", raw_text)) < 20 or image_area > 0
            if likely_scan or errors or any(b["quality"] == "review" for b in page_blocks):
                try:
                    preview = _render_pdf_page(path, number, cache, config)
                    previews.append(str(preview))
                    for block in page_blocks:
                        if likely_scan or errors:
                            block["quality"] = "review"
                        block["preview"] = str(preview)
                    if likely_scan:
                        recognized, failure = _ocr(preview, config)
                        for index, line in enumerate(recognized, 1):
                            page_blocks.append(_block(line["text"], "ocr", number, section, f"page:{number}/ocr:{index}", "review",
                                                      preview=str(preview), confidence=line["confidence"], ocr_box=line["ocr_box"],
                                                      low_confidence=line["confidence"] < config["confidence"]))
                        errors.append(f"第 {number} 页可能为扫描页、混合图片页或少量文字页，必须核对图像内容。" + (failure or "OCR 结果需要核对金额、日期、姓名和评分条件。"))
                    elif any(b["kind"] == "table" for b in page_blocks):
                        errors.append(f"第 {number} 页表格已提取，合并关系与跨页续表需要原页核对。")
                    if not page_blocks:
                        page_blocks.append(_block("", "ocr", number, section, f"page:{number}", "review", preview=str(preview)))
                except Exception as error:
                    errors.append(f"第 {number} 页预览渲染失败（{type(error).__name__}），请打开原始 PDF 人工核对。")
                    page_blocks.append(_block("", "ocr", number, section, f"page:{number}", "review"))
            if not page_blocks:
                page_blocks.append(_block("", page=number, section=section, locator=f"page:{number}", quality="review"))
                errors.append(f"第 {number} 页未提取到内容，不能判定无要求。")
            blocks.extend(sorted(page_blocks, key=lambda b: b.get("bbox", [0, 0])[1]))
            warnings.extend(errors)
        page_count = len(pdf.pages)
    repeated = collections.defaultdict(set)
    for block in edge_candidates:
        repeated[block["text"].strip()].add(block["page"])
    for block in edge_candidates:
        text = block["text"].strip()
        page_number = bool(re.fullmatch(r"[-—\s]*\d+[-—\s]*|第\s*\d+\s*页(?:\s*[共/／]\s*\d+\s*页)?", text))
        if page_number:
            block["excluded_from_clean"] = True
            block["noise_reason"] = "单独页码仅从清洗稿排除，原块保留。"
        elif page_count >= 3 and len(repeated[text]) >= max(3, page_count * .6):
            block["candidate_noise"] = True
            block["quality"] = "review"
            block["noise_reason"] = "重复页边文字候选；可能含实质性要求，核对前继续保留。"
    return {"blocks": blocks, "warnings": warnings, "page_count": page_count, "previews": previews}


def _parse_image(path: Path, cache: Path, config: dict) -> dict:
    from PIL import Image, ImageSequence
    blocks, warnings, previews = [], [], []
    with Image.open(path) as source:
        for number, frame in enumerate(ImageSequence.Iterator(source), 1):
            preview = cache / "pages" / f"page-{number:04d}.png"
            preview.parent.mkdir(parents=True, exist_ok=True)
            frame.convert("RGB").save(preview)
            previews.append(str(preview))
            recognized, failure = _ocr(preview, config)
            for index, line in enumerate(recognized, 1):
                blocks.append(_block(line["text"], "ocr", number, locator=f"image:{number}/ocr:{index}", quality="review",
                                     preview=str(preview), confidence=line["confidence"], ocr_box=line["ocr_box"],
                                     low_confidence=line["confidence"] < config["confidence"]))
            if not recognized:
                blocks.append(_block("", "ocr", number, locator=f"image:{number}", quality="review", preview=str(preview)))
            warnings.append(f"图像第 {number} 页：" + (failure or "OCR 已识别，金额、日期、姓名和评分条件仍需人工核对。"))
    return {"blocks": blocks, "warnings": warnings, "page_count": len(previews), "previews": previews}


def parse_document(path: Path, cache: Path, config: dict) -> dict:
    """输出文本、逐块来源及疑点；处理过程不执行文档内的任何指令。"""
    suffix = path.suffix.lower()
    try:
        if suffix in {".md", ".txt"}:
            result = _parse_text(path)
        elif suffix == ".docx":
            result = _parse_docx(path, cache, config)
        elif suffix == ".pdf":
            result = _parse_pdf(path, cache, config)
        else:
            result = _parse_image(path, cache, config)
    except Exception as error:
        result = {"blocks": [_block("", locator=path.name, quality="review")],
                  "warnings": [f"文件解析失败（{type(error).__name__}），原件已保留，请人工核对。"],
                  "page_count": None, "previews": []}
    result["status"] = "needs_review" if result["warnings"] or any(b["quality"] == "review" for b in result["blocks"]) else "parsed"
    return result
