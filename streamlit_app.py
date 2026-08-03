"""Streamlit Cloud entry point for the bank-statement relevance checker."""
from __future__ import annotations

import csv
from io import BytesIO, StringIO

import pandas as pd
import streamlit as st
from pypdf import PdfReader

from app import extract_pdf_text, parse_transactions

st.set_page_config(page_title="流水核验台", page_icon="📄", layout="wide")
st.markdown("""
<style>
  .stApp { background: #f6f8fb; }
  h1, h2, h3 { color: #172332; }
  [data-testid="stMetric"] { background: #fff; border: 1px solid #e7edf3; border-radius: 8px; padding: 12px; }
</style>
""", unsafe_allow_html=True)


def parse_pdf(content: bytes, industry: str, business: str) -> tuple[list[dict], int, int]:
    reader = PdfReader(BytesIO(content))
    if reader.is_encrypted:
        raise ValueError("该 PDF 已加密，请先导出无密码版本后再上传。")
    text = extract_pdf_text(reader)
    if len(text.strip()) < 30:
        raise ValueError("未从 PDF 提取到足够文字。这通常是扫描件，请先使用 OCR 转成可搜索 PDF。")
    transactions = parse_transactions(text, industry, business)
    if not transactions:
        raise ValueError("已读取 PDF 文本，但未识别到交易行。请提供脱敏样本以适配该银行模板。")
    return transactions, len(reader.pages), len(text)


def analyze(transactions: list[dict]) -> dict:
    high = sum(item["tag"] == "高" for item in transactions)
    low = sum(item["tag"] == "低" for item in transactions)
    score = max(20, min(95, round(55 + high / len(transactions) * 35 - low / len(transactions) * 25)))
    incoming = sum(item["amount"] for item in transactions if item["dir"] == "入账" and item["tag"] != "低")
    outgoing = sum(item["amount"] for item in transactions if item["dir"] == "出账" and item["tag"] != "低")
    return {"score": score, "high": high, "low": low, "incoming": incoming, "outgoing": outgoing}


def summarize_counterparties(transactions: list[dict]) -> pd.DataFrame:
    """按解析到的交易对手/摘要汇总往来频率和收支金额。"""
    frame = pd.DataFrame(transactions)
    frame["交易对手"] = frame["name"].fillna("未识别交易对手")
    frame["相关收入"] = frame.apply(lambda item: item["amount"] if item["dir"] == "入账" and item["tag"] != "低" else 0, axis=1)
    frame["相关支出"] = frame.apply(lambda item: item["amount"] if item["dir"] == "出账" and item["tag"] != "低" else 0, axis=1)
    summary = frame.groupby("交易对手", as_index=False).agg(交易笔数=("amount", "size"), 相关收入=("相关收入", "sum"), 相关支出=("相关支出", "sum"))
    summary["净额"] = summary["相关收入"] - summary["相关支出"]
    return summary.sort_values(["交易笔数", "相关收入", "相关支出"], ascending=False)


st.title("流水核验台")
st.caption("经营关联分析 · 上传文件仅用于本次处理")

with st.sidebar:
    st.subheader("经营信息")
    industry = st.selectbox("经营行业", ["餐饮", "商贸", "运输", "装修", "其他"])
    business = st.text_area("经营描述", placeholder="例如：经营社区早餐加盟店，主要向食材供应商采购，客户以零售和外卖平台收款为主。", height=150)
    st.caption("结果为尽调辅助意见，请结合合同、发票和现场调查复核。")

uploaded = st.file_uploader("上传银行流水或微信流水 PDF", type=["pdf"], help="仅支持可复制文字的 PDF；扫描件需先做 OCR。")
if uploaded is not None:
    st.caption(f"已选择：{uploaded.name} · {uploaded.size / 1024 / 1024:.2f} MB")

if st.button("开始关联分析", type="primary", disabled=uploaded is None):
    try:
        with st.spinner("正在读取并分析 PDF..."):
            transactions, pages, chars = parse_pdf(uploaded.getvalue(), industry, business)
            st.session_state["transactions"] = transactions
            st.session_state["meta"] = {"pages": pages, "chars": chars}
    except Exception as exc:
        st.error(str(exc))

if "transactions" in st.session_state:
    transactions = st.session_state["transactions"]
    stats = analyze(transactions)
    meta = st.session_state["meta"]
    level = "较高" if stats["score"] >= 70 else "一般" if stats["score"] >= 45 else "较低"
    st.divider()
    st.subheader(f"经营关联性：{level}（{stats['score']} / 100）")
    st.caption(f"已读取 {meta['pages']} 页，提取 {meta['chars']} 个字符，识别 {len(transactions)} 笔交易。")
    a, b, c, d = st.columns(4)
    a.metric("识别交易", f"{len(transactions)} 笔")
    b.metric("经营相关入账", f"¥{stats['incoming']:,.2f}")
    c.metric("经营相关出账", f"¥{stats['outgoing']:,.2f}")
    d.metric("关联不足/待核实", f"{stats['low']} 笔")

    st.subheader("交易对手分析")
    counterparties = summarize_counterparties(transactions)
    top_partner = counterparties.iloc[0]
    c1, c2, c3 = st.columns(3)
    c1.metric("交易对手数量", f"{len(counterparties)} 个")
    c2.metric("交易最频繁对象", str(top_partner["交易对手"]), f"{int(top_partner['交易笔数'])} 笔")
    c3.metric("交易最频繁对象相关净额", f"¥{top_partner['净额']:,.2f}")
    display_counterparties = counterparties.head(20).copy()
    for column in ["相关收入", "相关支出", "净额"]:
        display_counterparties[column] = display_counterparties[column].map(lambda value: f"¥{value:,.2f}")
    st.dataframe(display_counterparties, use_container_width=True, hide_index=True)
    selected_partner = st.selectbox("查看某一交易对手的往来明细", counterparties["交易对手"].tolist())

    trend = pd.DataFrame(transactions)
    trend["月份"] = pd.to_datetime(trend["date"], errors="coerce").dt.strftime("%Y-%m")
    trend = trend.dropna(subset=["月份"])
    trend["相关收入"] = trend.apply(lambda item: item["amount"] if item["dir"] == "入账" and item["tag"] != "低" else 0, axis=1)
    trend["相关支出"] = trend.apply(lambda item: item["amount"] if item["dir"] == "出账" and item["tag"] != "低" else 0, axis=1)
    monthly_trend = trend.groupby("月份")[["相关收入", "相关支出"]].sum().sort_index()
    if not monthly_trend.empty:
        st.caption("按月相关流水趋势")
        st.bar_chart(monthly_trend)

    st.subheader("相关流水")
    rows = [{
        "日期": item["date"], "交易对手/摘要": item["name"], "金额": f"{'+' if item['dir'] == '入账' else '-'}¥{item['amount']:,.2f}",
        "方向": item["dir"], "经营关联": {"高": "强相关", "中": "可能相关", "低": "关联不足"}[item["tag"]], "判定依据": item["reason"],
    } for item in transactions]
    selected_partner_rows = [row for row in rows if row["交易对手/摘要"] == selected_partner]
    with st.expander(f"查看 {selected_partner} 的交易明细"):
        st.dataframe(pd.DataFrame(selected_partner_rows), use_container_width=True, hide_index=True)
    related_rows = [row for row in rows if row["经营关联"] != "关联不足"]
    related_filter = st.radio("筛选范围", ["全部相关流水", "收入相关流水", "支出相关流水"], horizontal=True)
    if related_filter == "收入相关流水":
        related_rows = [row for row in related_rows if row["方向"] == "入账"]
    elif related_filter == "支出相关流水":
        related_rows = [row for row in related_rows if row["方向"] == "出账"]
    st.caption(f"当前显示 {len(related_rows)} 笔相关流水")
    table = pd.DataFrame(related_rows, columns=rows[0].keys())

    def highlight_strong_related(row):
        if row["经营关联"] == "强相关":
            return ["background-color: #fff0ee; color: #b42318; font-weight: 700"] * len(row)
        return [""] * len(row)

    st.dataframe(table.style.apply(highlight_strong_related, axis=1), use_container_width=True, hide_index=True, height=600)

    with st.expander("查看全部交易明细与判定"):
        st.dataframe(pd.DataFrame(rows).style.apply(highlight_strong_related, axis=1), use_container_width=True, hide_index=True, height=600)

    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    st.download_button("下载分析明细 CSV", data="\ufeff" + buffer.getvalue(), file_name="流水关联分析.csv", mime="text/csv")
    st.warning("本结果不构成授信审批结论。请结合账户完整性、合同/发票及人工尽调进行复核。")
