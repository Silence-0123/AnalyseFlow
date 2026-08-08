"""本地流水核验工具：不保存上传文件，仅在内存中解析文本型 PDF。"""
from __future__ import annotations

import re
from collections import Counter
from io import BytesIO
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory
from pypdf import PdfReader

ROOT = Path(__file__).parent
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

KEYWORDS = {
    "餐饮": ("美团|饿了么|外卖|餐饮|食品|食材|农贸|菜市场|米|面|油|燃气|餐具|配送", "娱乐城|棋牌|彩票|证券|股票"),
    "商贸": ("批发|商贸|供应链|采购|物流|仓储|货款|百货|商城", "娱乐城|棋牌|彩票|证券|股票"),
    "运输": ("货运|物流|运输|油站|加油|路桥|高速|停车|运费|车队", "娱乐城|棋牌|彩票|证券|股票"),
    "装修": ("建材|装饰|装修|五金|涂料|水泥|瓷砖|工程|施工", "娱乐城|棋牌|彩票|证券|股票"),
    "其他": ("租金|水费|电费|燃气|采购|货款|结算|收款", "娱乐城|棋牌|彩票|证券|股票"),
}
YEAR = r"(?:202[0-9]|203[0-5])"
MONTH = r"(?:0[1-9]|1[0-2])"
DAY = r"(?:0[1-9]|[12]\d|3[01])"
# 严格限定合理日期，避免把交易单号中的 20812026、20262604 等数字片段当作日期。
DATE = re.compile(rf"(?<!\d)(?:{YEAR}[/-]{MONTH}[/-]{DAY}|{YEAR}年\d{{1,2}}月\d{{1,2}}日|{YEAR}{MONTH}{DAY})(?!\d)")
# 流水金额统一要求两位小数，交易单号、交易时间和手机号不会再被误认作金额。
CURRENCY = re.compile(r"(?<![\d.])([+-]?\d{1,3}(?:,\d{3})*\.\d{2})(?![\d.])")


def money(value: str) -> float | None:
    try:
        value = value.replace(",", "")
        number = float(value)
        return number if 0.01 <= abs(number) <= 100_000_000 else None
    except ValueError:
        return None


def normalize_date(value: str) -> str:
    """统一银行常见的 20250719、2025-07-19、2025年7月19日 日期格式。"""
    if re.fullmatch(r"20\d{6}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value.replace("年", "-").replace("月", "-").replace("日", "")


def extract_pdf_text(reader: PdfReader) -> str:
    """按坐标重建表格行，避免 PDF 内部按列存储文字导致字段错位。"""
    pages: list[str] = []
    for page in reader.pages:
        fragments: list[tuple[float, float, str]] = []

        def capture(text, _cm, tm, _font, _size):
            clean = " ".join(text.split())
            if clean:
                fragments.append((float(tm[5]), float(tm[4]), clean))

        page.extract_text(visitor_text=capture)
        fragments.sort(key=lambda item: (-item[0], item[1]))
        rows: list[list[tuple[float, float, str]]] = []
        for fragment in fragments:
            if not rows or abs(rows[-1][0][0] - fragment[0]) > 2.5:
                rows.append([fragment])
            else:
                rows[-1].append(fragment)
        pages.append("\n".join(" ".join(cell[2] for cell in sorted(row, key=lambda item: item[1])) for row in rows))
    return "\n".join(pages)


def classify(text: str, direction: str, industry: str, description: str) -> tuple[str, str]:
    combined = text + " " + description
    good, bad = KEYWORDS.get(industry, KEYWORDS["其他"])
    if re.search(bad, combined, re.I):
        return "低", "命中非经营性高风险关键词，需核实交易背景"
    if re.search(good, combined, re.I):
        return "高", "交易摘要命中行业经营关键词"
    if re.search(r"贷款|借款|利息|信用卡|还款|理财|基金|保险|红包|转账", text, re.I):
        return "低", "资金用途或来源偏个人/金融属性，未见经营关联依据"
    if re.search(r"租金|房租|水费|电费|燃气|物业|税费|社保|工资|货款|采购", text, re.I):
        return "中", "具备常见经营成本特征，仍需凭证或对手方信息确认"
    if re.search(r"微信收款|支付宝收款|银联收款|POS|转账", text, re.I) and direction == "入账":
        return "中", "小微商户常见收款渠道，建议结合订单或收银记录核验"
    if re.search(r"微信支付|支付宝|财付通|快捷支付|手机银行|电子支付", text, re.I):
        return "低", "通用支付渠道未显示经营用途，不能仅据此认定为经营交易"
    return "低", "未发现行业、商户或经营成本特征，关联依据不足"


def parse_transactions(text: str, industry: str, description: str) -> list[dict]:
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    # 不同银行的 PDF 可能把一笔交易拆为日期、摘要、金额等多行。既识别
    # 单行，也将每个日期到下一个日期之间的文字组合为一个候选交易块。
    # 只解析完整交易块，避免微信流水单元格换行时截断交易对手名称。
    candidates = []
    for index, line in enumerate(lines):
        if not DATE.search(line):
            continue
        end = min(index + 8, len(lines))
        for next_index in range(index + 1, end):
            if DATE.search(lines[next_index]):
                end = next_index
                break
        candidates.append(" ".join(lines[index:end]))
    result: list[dict] = []
    seen: set[tuple[str, float, str]] = set()
    for line in candidates:
        date = DATE.search(line)
        if not date:
            continue
        # 日期中的年、月、日也是数字，先将其移除，避免被误当成金额。
        amount_source = line[:date.start()] + " " + line[date.end():]
        amounts = [money(m.group(1)) for m in CURRENCY.finditer(amount_source)]
        amounts = [x for x in amounts if x is not None]
        if not amounts:
            continue
        # 农行等明细以“交易金额、余额”连续列出，前者才是本笔金额；
        # 带符号的首个金额也优先作为交易金额。
        is_compact_abc_date = bool(re.fullmatch(r"20\d{6}", date.group(0)))
        signed = re.search(r"(?<![\d.])([+-]\d{1,3}(?:,\d{3})*\.\d{2})(?![\d.])", amount_source)
        amount = money(signed.group(1)) if signed else (amounts[0] if is_compact_abc_date else amounts[-1])
        if amount is None:
            continue
        is_in = bool(re.search(r"收入|入账|贷|收款|转入|存入", line))
        is_out = bool(re.search(r"支出|出账|借|付款|转出|扣款|消费", line))
        direction = "入账" if is_in and not is_out else "出账" if is_out else ("出账" if amount < 0 else "入账")
        amount = abs(amount)
        name = line[date.end():]
        # 农行格式的下一列为六位交易时间，移除它以获得更清晰的交易摘要。
        name = re.sub(r"^\s*\d{6}\s*", "", name)
        name = re.sub(r"[+-]?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?", "", name).strip(" -|：:")
        # PDF 表格跨行合并时可能在单元格边界产生 “::” 或 “：：”。
        name = re.sub(r"\s*[:：]+\s*", "", name)
        name = name[:80] or "未识别交易摘要"
        key = (normalize_date(date.group(0)), amount, name)
        if key in seen:
            continue
        seen.add(key)
        tag, reason = classify(name, direction, industry, description)
        result.append({"date": normalize_date(date.group(0)), "name": name, "amount": amount, "dir": direction, "tag": tag, "reason": reason})
    # 保持 PDF 原始交易顺序并完整返回；个人本地版不应静默截断明细。
    return result


@app.get("/")
def home():
    return send_from_directory(ROOT, "index.html")


@app.get("/<path:filename>")
def asset(filename: str):
    """仅暴露页面需要的静态文件，避免把项目目录当作下载目录。"""
    if filename not in {"styles.css", "overrides.css", "app.js"}:
        abort(404)
    return send_from_directory(ROOT, filename)


@app.post("/api/analyze")
def analyze():
    upload = request.files.get("file")
    if not upload or not upload.filename.lower().endswith(".pdf"):
        return jsonify(error="请选择 PDF 格式的流水文件。"), 400
    try:
        reader = PdfReader(BytesIO(upload.read()))
        if reader.is_encrypted:
            return jsonify(error="该 PDF 已加密，请先导出无密码版本后再上传。"), 400
        text = extract_pdf_text(reader)
    except Exception as exc:
        return jsonify(error=f"PDF 无法读取：{exc}"), 400
    if len(text.strip()) < 30:
        return jsonify(error="未从 PDF 提取到足够文字。这通常是扫描件，请先使用 OCR 转成可搜索 PDF。"), 422
    industry = request.form.get("industry", "其他")
    description = request.form.get("business", "")
    transactions = parse_transactions(text, industry, description)
    if not transactions:
        return jsonify(error="已读取 PDF 文本，但未识别到交易行。不同银行版式差异较大，需要补充该模板的解析规则。"), 422
    high = sum(t["tag"] == "高" for t in transactions)
    low = sum(t["tag"] == "低" for t in transactions)
    score = max(35, min(95, round(55 + high / len(transactions) * 35 - low / len(transactions) * 25)))
    return jsonify(transactions=transactions, score=score, extracted_chars=len(text), pages=len(reader.pages))


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="文件超过 25 MB 限制。"), 413


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
