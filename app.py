import html
import hmac
import io
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import altair as alt
import streamlit as st
from PIL import Image, ImageStat, UnidentifiedImageError
from rapidocr import RapidOCR

from audit_store import (
    REVIEWABLE_STATUSES,
    create_ticket as persist_ticket,
    initialize_database,
    list_review_logs,
    list_tickets,
    save_human_decision,
    search_similar_cases,
)
from llm_agent import ReviewLLMAgent
from models import ReviewViolation, StructuredReviewReport
from review_graph import ReviewDeps, build_review_graph, run_review
from policy_retrieval import (
    citations_for_findings,
    search_policy_query,
    unsupported_rule_ids,
    validate_evidence_bindings,
)


st.set_page_config(
    page_title="Creative Compliance Copilot",
    page_icon="C",
    layout="wide",
    initial_sidebar_state="auto",
)


POLICIES = [
    {"id": "COS-ABS-001", "industry": "护肤品", "category": "功效宣称", "title": "绝对化与保证性效果", "text": "不得使用第一、最好、100%、彻底、永久等绝对化或保证性表述；具体效果周期及全部人群承诺应有充分依据。", "source": "法规基线：广告法第九条、第二十八条", "effective": "2026-01-01", "level": "高", "basis": "法规基线"},
    {"id": "COS-MED-002", "industry": "护肤品", "category": "行业准入", "title": "不得暗示医疗功效", "text": "普通化妆品不得宣称治疗、根治疾病或替代医疗；功效宣称应有相应评价依据并与产品属性相符。", "source": "法规基线：化妆品监督管理条例、功效宣称评价规范", "effective": "2026-01-01", "level": "高", "basis": "法规基线"},
    {"id": "PLAT-CLEAR-003", "industry": "通用", "category": "真实性", "title": "素材表达真实清晰", "text": "广告内容应与商品实际能力及落地页信息一致，不得通过夸张对比、模糊限定条件或结果导向语言造成误导。", "source": "模拟平台执行规则（基于广告法第二十八条）", "effective": "2026-01-01", "level": "中", "basis": "模拟平台规则"},
    {"id": "PLAT-LP-004", "industry": "通用", "category": "落地页", "title": "广告与落地页一致", "text": "素材中的价格、优惠、产品名称和核心利益点应能在落地页获得明确支持，优惠限制条件应清晰展示。", "source": "模拟平台执行规则（基于广告法真实性要求）", "effective": "2026-01-01", "level": "高", "basis": "模拟平台规则"},
    {"id": "BRAND-TONE-005", "industry": "通用", "category": "品牌规则", "title": "品牌语气与禁用表达", "text": "品牌物料不得使用贬低用户、制造容貌焦虑或无条件催促购买的表达。", "source": "企业内部品牌手册 v3.2（模拟）", "effective": "2026-03-01", "level": "中", "basis": "企业规则"},
    {"id": "FOOD-FUNC-006", "industry": "食品饮料", "category": "功效宣称", "title": "普通食品不得宣称保健治疗功能", "text": "普通食品不得宣传疾病预防、治疗、减肥或提高免疫力等未经许可的保健功能。", "source": "法规基线：食品安全法、广告法相关规定", "effective": "2026-01-01", "level": "高", "basis": "法规基线"},
    {"id": "EDU-GUAR-007", "industry": "教育培训", "category": "效果承诺", "title": "不得保证培训结果", "text": "不得对升学、考试通过、就业和收入提升作出明示或暗示的保证性承诺。", "source": "法规基线：广告法第二十四条及教育培训广告监管提醒", "effective": "2026-01-01", "level": "高", "basis": "法规基线"},
    {"id": "FIN-RETURN-008", "industry": "金融服务", "category": "收益风险", "title": "不得承诺收益或弱化风险", "text": "不得以保本保收益、零风险等方式作确定性承诺；金融广告应按具体业务监管要求展示风险提示。", "source": "模拟行业规则（基于金融广告监管原则，需法务复核）", "effective": "2026-01-01", "level": "高", "basis": "行业规则"},
    {"id": "PLAT-ASSET-009", "industry": "通用", "category": "素材规格", "title": "图片素材基本要求", "text": "单张图片应为 JPG、JPEG 或 PNG，文件不超过 10MB，画面与文字需清晰可辨。", "source": "模拟平台素材规格（非任何平台内部资料）", "effective": "2026-01-01", "level": "中", "basis": "模拟平台规则"},
]

LEGAL_BASES = [
    {"name": "《中华人民共和国广告法》", "authority": "全国人大 / 市场监管总局公开法规", "focus": "绝对化用语、虚假或引人误解内容、教育培训承诺", "url": "https://www.samr.gov.cn/"},
    {"name": "《化妆品功效宣称评价规范》", "authority": "国家药品监督管理局", "focus": "化妆品功效宣称与评价依据", "url": "https://www.nmpa.gov.cn/"},
    {"name": "《化妆品监督管理条例》", "authority": "中国政府网 / 国家药监局", "focus": "化妆品产品属性、备案与宣称边界", "url": "https://www.gov.cn/"},
    {"name": "教育培训广告监管公开提醒", "authority": "市场监管部门公开执法提醒", "focus": "不得对考试、升学、就业结果作保证", "url": "https://www.samr.gov.cn/"},
]

CASES = [
    {"id": "CASE-032", "copy": "7天祛痘，学生党闭眼入", "industry": "护肤品", "risk": "高", "result": "修改后提交"},
    {"id": "CASE-031", "copy": "全网最低价，错过再等一年", "industry": "食品饮料", "risk": "高", "result": "人工审核"},
    {"id": "CASE-030", "copy": "轻盈保湿，适合日常护理", "industry": "护肤品", "risk": "低", "result": "通过"},
    {"id": "CASE-029", "copy": "报班保证上岸，不过全退", "industry": "教育培训", "risk": "高", "result": "驳回"},
    {"id": "CASE-028", "copy": "历史年化8%，稳健投资之选", "industry": "金融服务", "risk": "高", "result": "人工审核"},
]

RULE_CHECKS = [
    {"pattern": r"\d+天|立刻|马上|当场见效", "category": "明确周期承诺", "reason": "把明确时间与效果绑定，容易构成无法验证的保证性承诺。", "severity": "高", "policy": "COS-ABS-001", "industries": ["护肤品"]},
    {"pattern": r"所有|彻底|100%|永久|第一|最好|顶级|全网最低|闭眼入", "category": "绝对化表达", "reason": "使用覆盖全部结果或无条件推荐的词语，缺少必要限定条件。", "severity": "高", "policy": "COS-ABS-001", "industries": ["护肤品", "食品饮料", "教育培训", "金融服务"]},
    {"pattern": r"消除|根治|祛除|治疗|药到病除|医学级", "category": "疑似医疗功效", "reason": "普通护肤品不应暗示治疗或消除疾病，应回到日常护理语境。", "severity": "高", "policy": "COS-MED-002", "industries": ["护肤品"]},
    {"pattern": r"婴儿般|换脸|重获新生|丑|老女人|黄脸婆", "category": "夸张或容貌焦虑", "reason": "极端效果类比或负面标签可能放大容貌焦虑并误导消费者。", "severity": "中", "policy": "BRAND-TONE-005", "industries": ["护肤品"]},
    {"pattern": r"减肥|降血糖|抗癌|提高免疫力|治疗", "category": "普通食品功效越界", "reason": "普通食品不应宣称未经许可的保健或疾病治疗功效。", "severity": "高", "policy": "FOOD-FUNC-006", "industries": ["食品饮料"]},
    {"pattern": r"保证上岸|保过|包过|必进名校|保证就业|不过全退", "category": "培训结果保证", "reason": "对考试、升学或就业结果作出明示或组合式保证。", "severity": "高", "policy": "EDU-GUAR-007", "industries": ["教育培训"]},
    {"pattern": r"保本|稳赚|零风险|固定收益|保证收益|年化\d+%", "category": "收益承诺", "reason": "对投资结果作确定性承诺，或未同时充分揭示本金损失风险。", "severity": "高", "policy": "FIN-RETURN-008", "industries": ["金融服务"]},
    {"pattern": r"最后一天|不买就亏|错过再等|必须买", "category": "过度催促购买", "reason": "使用强迫性或虚假稀缺语言，不符合品牌克制、可信的表达要求。", "severity": "中", "policy": "BRAND-TONE-005", "industries": ["护肤品", "食品饮料", "教育培训", "金融服务"]},
]

# 规则和法规来源均保存在 data/，页面只负责加载和执行。
BASE_DIR = Path(__file__).resolve().parent
with (BASE_DIR / "data" / "rules.json").open("r", encoding="utf-8") as file:
    CATALOG_RULES = json.load(file)
with (BASE_DIR / "data" / "sources.json").open("r", encoding="utf-8") as file:
    SOURCES = json.load(file)
LEGAL_INDEX_PATH = BASE_DIR / "data" / "legal_index.json"
LEGAL_INDEX = json.loads(LEGAL_INDEX_PATH.read_text(encoding="utf-8")) if LEGAL_INDEX_PATH.exists() else []
EXCERPTS_PATH = BASE_DIR / "data" / "legal_excerpts.json"
LEGAL_EXCERPTS = json.loads(EXCERPTS_PATH.read_text(encoding="utf-8")) if EXCERPTS_PATH.exists() else []

for rule in CATALOG_RULES:
    if not any(policy["id"] == rule["id"] for policy in POLICIES):
        POLICIES.append({
            "id": rule["id"],
            "industry": rule["industry"],
            "category": rule["category"],
            "title": rule["title"],
            "text": rule["reason"],
            "source": rule["legal_basis"],
            "effective": "2026-07-12",
            "level": rule["severity"],
            "basis": rule["basis"],
            "action": rule["action"],
        })

ALL_INDUSTRIES = ["护肤品", "食品饮料", "宠物用品", "电子产品", "日用品"]
RULE_CHECKS.extend([
    {
        "pattern": rule["pattern"],
        "category": rule["title"],
        "reason": rule["reason"],
        "severity": rule["severity"],
        "policy": rule["id"],
        "industries": ALL_INDUSTRIES if rule["industry"] == "通用" else [rule["industry"]],
    }
    for rule in CATALOG_RULES
    if rule.get("pattern")
])

# Activate the supported retail categories plus the shared review controls.
CORE_RULE_IDS = {
    "COS-ABS-001", "COS-MED-002", "PLAT-CLEAR-003", "PLAT-LP-004",
    "BRAND-TONE-005", "PLAT-ASSET-009",
}
ACTIVE_RULE_IDS = CORE_RULE_IDS | {
    rule["id"] for rule in CATALOG_RULES
    if rule["industry"] == "通用" or rule["industry"] in ALL_INDUSTRIES
}
POLICIES = [policy for policy in POLICIES if policy["id"] in ACTIVE_RULE_IDS]
RULE_CHECKS = [check for check in RULE_CHECKS if check["policy"] in ACTIVE_RULE_IDS]
RULE_CHECKS.extend([
    {"pattern": r"敏感肌.*(?:适用|可用|放心)|孕妇.*(?:可用|使用)", "category": "特定人群适用宣称", "reason": "敏感肌、孕妇等特定人群适用范围需要产品安全评价、标签和使用条件支持。", "severity": "中", "policy": "COS-008", "industries": ["护肤品"]},
    {"pattern": r"皮肤科医生.*推荐|院线同款|三甲医院推荐|专家力荐", "category": "医疗权威或专家背书", "reason": "需要核验推荐主体身份、授权、适用范围及广告代言合规性。", "severity": "高", "policy": "COS-009", "industries": ["护肤品"]},
    {"pattern": r"经测试|测试表明", "category": "测试结论缺少出处", "reason": "测试或评价结论需要标明机构、样本、方法、周期和适用条件。", "severity": "中", "policy": "COS-010", "industries": ["护肤品"]},
    {"pattern": r"SPF\d+\+?|防晒", "category": "防晒功效资质核验", "reason": "防晒属于需要重点核验的化妆品功效，应与注册备案和检测资料一致。", "severity": "高", "policy": "COS-002", "industries": ["护肤品"]},
    {"pattern": r"防脱|生发", "category": "防脱发功效或医疗化风险", "reason": "防脱发宣称需核验特殊化妆品注册及评价依据，生发暗示还可能越界为医疗功效。", "severity": "高", "policy": "COS-001", "industries": ["护肤品"]},
])

DEFAULT_COPY = "7天淡化所有斑点，让你重获婴儿般肌肤。现在下单仅需199元。"
DEFAULT_LANDING = "透亮修护精华，日常补水保湿。活动价239元，实际效果因人而异。"

DEMO_REVIEWERS = {
    "reviewer.a": {"name": "审核员 A", "role": "广告审核员", "team": "综合审核一组"},
    "reviewer.b": {"name": "审核员 B", "role": "高级审核员", "team": "综合审核二组"},
    "risk.ops": {"name": "风控专员 E", "role": "风控复核员", "team": "高风险复核组"},
}

TOOL_DISPLAY_NAMES = {
    "llm_plan_tools": "模型工具规划",
    "classify_ad_industry": "行业识别",
    "check_forbidden_words": "确定性规则检查",
    "retrieve_applicable_policy": "政策依据检索",
    "search_similar_cases": "相似案例检索",
    "analyze_semantic_risk": "语义风险分析",
    "generate_compliant_rewrite": "合规整改建议",
}

SEED_TICKETS = json.loads((BASE_DIR / "data" / "tickets.json").read_text(encoding="utf-8"))


def init_state():
    defaults = {
        "scan_count": 128,
        "current_report": None,
        "selected_variant": "最小修改版",
        "current_user": None,
        "selected_ticket_id": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def clear_reviewer_session():
    sensitive_keys = {
        "current_user", "selected_ticket_id", "current_report",
        "current_workflow", "ocr_text", "created_ticket",
        "last_saved_decision", "workbench_workflows",
    }
    widget_prefixes = (
        "human_review_note_", "human_confirm_state_", "task_library_selection",
    )
    for key in list(st.session_state):
        if key in sensitive_keys or key.startswith(widget_prefixes):
            del st.session_state[key]
    init_state()


def authenticate_demo_reviewer(account, password):
    expected_password = os.getenv("DEMO_REVIEW_PASSWORD", "demo123")
    reviewer = DEMO_REVIEWERS.get(account.strip().lower())
    if reviewer and hmac.compare_digest(password, expected_password):
        return {"account": account.strip().lower(), **reviewer, "auth_source": "企业 SSO（模拟）"}
    return None


def policy_by_id(policy_id):
    policy = next((p for p in POLICIES if p["id"] == policy_id), None)
    if policy is None:
        raise KeyError(f"Unknown policy id: {policy_id}")
    return policy


@st.cache_resource
def get_ocr_engine():
    return RapidOCR()


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG"}


@st.cache_data(show_spinner=False, max_entries=32)
def validate_image_bytes(image_bytes):
    if not image_bytes:
        raise ValueError("图片文件为空。")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("图片超过 10MB，请压缩后重新上传。")
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.verify()
        with Image.open(io.BytesIO(image_bytes)) as image:
            image_format = (image.format or "").upper()
            width, height = image.size
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError("文件不是可解析的 JPG/PNG 图片，或图片已经损坏。") from error
    if image_format not in ALLOWED_IMAGE_FORMATS:
        raise ValueError(f"实际图片格式为 {image_format or '未知'}，仅支持 JPG/PNG。")
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise ValueError("图片像素总量超过 2500 万，请降低分辨率后重试。")
    return {"format": image_format, "width": width, "height": height, "bytes": len(image_bytes)}


@st.cache_data(show_spinner=False, max_entries=32)
def extract_text_from_image(image_bytes):
    validate_image_bytes(image_bytes)
    try:
        output = get_ocr_engine()(image_bytes)
        texts = list(output.txts or ())
        scores = list(output.scores or ())
        confidence = sum(scores) / len(scores) if scores else 0.0
        return "\n".join(texts), confidence
    except Exception as error:
        raise RuntimeError("OCR 处理失败，请由审核员检查原图或重新上传。") from error


@st.cache_data(show_spinner=False, max_entries=32)
def analyze_image_visual(image_bytes):
    validate_image_bytes(image_bytes)
    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        width, height = image.size
        brightness = ImageStat.Stat(image.convert("L")).mean[0]
    except Exception as error:
        raise RuntimeError("图片视觉分析失败，请人工检查素材。") from error
    warnings = []
    if min(width, height) < 600:
        warnings.append("分辨率偏低，文字和免责声明可能难以辨认")
    if brightness < 45:
        warnings.append("画面整体偏暗，需人工检查关键信息可读性")
    if width / max(1, height) > 2.2 or height / max(1, width) > 2.2:
        warnings.append("长宽比较极端，可能不适配部分广告位")
    return {"width": width, "height": height, "brightness": round(brightness, 1), "warnings": warnings}


def retrieve_applicable_policy(query, findings, limit=3):
    del query  # Findings use deterministic evidence bindings, not fuzzy search.
    return citations_for_findings(findings, limit=limit)


def add_finding(findings, category, evidence, reason, severity, policy, source="文案"):
    key = (category, evidence, source)
    if not any((f["category"], f["evidence"], f["source"]) == key for f in findings):
        findings.append({"category": category, "evidence": evidence, "reason": reason, "severity": severity, "policy_id": policy, "source": source})


def classify_ad_industry(copy, product, selected_industry):
    if selected_industry in ALL_INDUSTRIES:
        return selected_industry, 1.0, "人工选择"
    text = f"{copy}{product}"
    term_groups = {
        "护肤品": ("肌肤", "精华", "护肤", "美白", "淡斑", "保湿", "防晒", "祛痘"),
        "食品饮料": ("食品", "饮料", "零食", "蛋白", "饮品", "营养", "燕麦", "果汁", "牛奶"),
        "宠物用品": ("宠物", "猫粮", "狗粮", "犬粮", "猫砂", "磨牙", "喂食", "兽药"),
        "电子产品": ("耳机", "手机", "充电", "续航", "防水", "蓝牙", "电池", "接口"),
        "日用品": ("洗衣液", "清洁剂", "除菌", "洗洁精", "垃圾袋", "收纳", "家居", "日用"),
    }
    scores = {industry: sum(term in text for term in terms) for industry, terms in term_groups.items()}
    industry, score = max(scores.items(), key=lambda item: item[1])
    if score == 0:
        return "待人工确认", 0.0, "未识别"
    return industry, min(0.95, 0.55 + score * 0.1), "关键词辅助识别"


def check_forbidden_words(copy, industry):
    matches = []
    for check in RULE_CHECKS:
        if industry not in check["industries"]:
            continue
        for match in re.finditer(check["pattern"], copy, re.IGNORECASE):
            matches.append({**check, "evidence": match.group(0)})
    return matches


def analyze_semantic_risk(copy):
    semantic_patterns = {
        "极端结果暗示": r"婴儿般|换脸|逆龄|冻龄|返老还童",
        "无条件适用暗示": r"所有人|任何肤质|人人适用|怎么用都有效",
    }
    return [
        {"category": category, "evidence": match.group(0)}
        for category, pattern in semantic_patterns.items()
        if (match := re.search(pattern, copy, re.IGNORECASE))
    ]


def extract_prices(text):
    return {int(value) for value in re.findall(r"(?<!\d)(\d{2,6})(?:元|块)", text)}


def scan_material(copy, landing, industry, product, audience, image_size, qualification, brand_terms):
    findings = []
    industry, industry_confidence, industry_source = classify_ad_industry(copy, product, industry)
    if industry == "待人工确认":
        add_finding(
            findings, "行业分类待确认", product or copy[:20],
            "无法可靠识别广告行业，规则范围可能不完整，需审核员先确认行业。",
            "高", "PLAT-CLEAR-003", "行业分类",
        )
    for match in check_forbidden_words(copy, industry):
        add_finding(findings, match["category"], match["evidence"], match["reason"], match["severity"], match["policy"])
    for semantic in analyze_semantic_risk(copy):
        if not any(finding["evidence"] == semantic["evidence"] for finding in findings):
            add_finding(
                findings, semantic["category"], semantic["evidence"],
                "该表达隐含无法稳定验证的效果或适用范围，需要结合素材语境人工确认。",
                "中", "PLAT-CLEAR-003", "语义分析",
            )

    copy_prices, landing_prices = extract_prices(copy), extract_prices(landing)
    if copy_prices and landing_prices and copy_prices != landing_prices:
        add_finding(findings, "价格信息不一致", f"素材 {min(copy_prices)} 元 / 落地页 {min(landing_prices)} 元", "广告素材与落地页展示价格不同，可能造成价格误导。", "高", "PLAT-LP-004", "一致性")
    elif copy_prices and not landing_prices:
        add_finding(findings, "优惠缺少落地页支持", f"素材价格 {min(copy_prices)} 元", "素材中的优惠价格未在落地页说明，需补充活动条件和有效期。", "中", "PLAT-LP-004", "一致性")

    if product and landing and product not in landing:
        add_finding(findings, "商品名称未对齐", product, "落地页未出现素材中的完整商品名称，建议确认是否为同一商品。", "中", "PLAT-LP-004", "一致性")

    custom_terms = [term.strip() for term in re.split(r"[,，、\n]", brand_terms) if term.strip()]
    for term in custom_terms:
        if term in copy:
            add_finding(findings, "品牌禁用词", term, "命中当前品牌配置的禁用表达，需要品牌管理员确认或替换。", "中", "BRAND-TONE-005", "品牌规则")

    regulated = industry in ["金融服务", "教育培训"]
    if regulated and not qualification:
        add_finding(findings, "行业资质缺失", f"{industry}资质文件", "当前行业属于重点审核范围，提交前需上传并核验主体或业务资质。", "高", "PLAT-CLEAR-003", "资质")

    if image_size and image_size > 10 * 1024 * 1024:
        add_finding(findings, "图片文件过大", f"{image_size / 1024 / 1024:.1f}MB", "图片超过平台模拟规格上限 10MB，请压缩后重新上传。", "中", "PLAT-ASSET-009", "素材规格")

    high = sum(f["severity"] == "高" for f in findings)
    medium = sum(f["severity"] == "中" for f in findings)
    score = min(99, high * 24 + medium * 11 + (6 if not image_size else 0))
    decision = "人工审核" if high >= 1 or regulated and not qualification else ("修改后提交" if findings else "通过")
    return {
        "report_id": f"PR-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "industry": industry,
        "industry_confidence": industry_confidence,
        "industry_source": industry_source,
        "product": product,
        "audience": audience,
        "copy": copy,
        "landing_page_text": landing,
        "qualification_verified": bool(qualification),
        "brand_terms": brand_terms,
        "findings": findings,
        "risk_score": score,
        "decision": decision,
        "human_review_required": decision == "人工审核",
        "tools": ["素材解析", "硬规则检查", "规则检索", "一致性检查", "品牌规则查询", "受约束改写"],
    }


def rewrite_variants(copy, product, audience, findings):
    del audience
    product_label = product or "该商品"
    direct_findings = [
        finding
        for finding in findings
        if finding["source"] in {"文案", "品牌规则", "语义分析", "LLM 语义分析"}
        and str(finding["evidence"]) in copy
    ]
    direct_evidence = {str(finding["evidence"]) for finding in direct_findings}
    price_evidence = {
        str(finding["evidence"])
        for finding in direct_findings
        if "价格" in finding["category"] or "优惠" in finding["category"]
    }
    has_price_risk = any(
        "价格" in finding["category"] or "优惠" in finding["category"]
        for finding in findings
    )
    safe_sentences = []
    retained_claim = False
    for sentence in re.findall(r"[^。！？!?]+[。！？!?]?", copy):
        sentence = sentence.strip()
        if not sentence:
            continue
        risky_evidence = [evidence for evidence in direct_evidence if evidence in sentence]
        has_price_phrase = bool(re.search(r"(?:现在下单)?仅需\s*\d+(?:\.\d+)?(?:元|块)", sentence))
        non_price_risk = [
            evidence for evidence in risky_evidence
            if evidence not in price_evidence
        ]
        if non_price_risk:
            continue
        if has_price_phrase or (has_price_risk and any(char.isdigit() for char in sentence)):
            safe_sentences.append("活动价格、期限和适用条件请以商品页面公示为准。")
            continue
        safe_sentences.append(sentence)
        retained_claim = True

    if not retained_claim and findings:
        safe_sentences.insert(0, f"{product_label}：[请填写经产品资料验证的卖点、适用条件和使用体验]。")
    clean = "".join(dict.fromkeys(safe_sentences)).strip() or (
        f"{product_label}：[请填写经产品资料验证的卖点、适用条件和使用体验]。"
    )
    return {
        "最小修改版": clean,
        "事实占位版": f"{product_label}：[请填写经产品资料验证的成分、使用体验或适用条件]。",
        "整改说明版": "请删除高风险承诺；价格、功效数据和适用人群须补充可核验依据后再提交。",
    }


def generate_compliant_rewrite(copy, product, audience, findings):
    return rewrite_variants(copy, product, audience, findings)


def highlight_copy(copy, findings):
    safe = html.escape(copy)
    evidences = sorted({f["evidence"] for f in findings if f["source"] == "文案"}, key=len, reverse=True)
    for evidence in evidences:
        escaped = html.escape(evidence)
        safe = safe.replace(escaped, f'<mark title="风险证据">{escaped}</mark>')
    return safe


def report_json(report, selected_copy):
    severity_map = {"高": "high", "中": "medium", "低": "low"}
    decision_map = {"通过": "pass", "修改后提交": "modify", "人工审核": "escalate"}
    violations = [
        ReviewViolation(
            category=finding["category"], evidence=str(finding["evidence"]),
            reason=finding["reason"], rule_id=finding["policy_id"],
            severity=severity_map.get(finding["severity"], "medium"),
            confidence=0.96 if finding["source"] in {"文案", "资质", "素材规格"} else 0.88,
        )
        for finding in report["findings"]
    ]
    risk_level = "high" if any(item.severity == "high" for item in violations) else ("medium" if violations else "low")
    payload = StructuredReviewReport(
        decision=decision_map.get(report["decision"], "escalate"),
        risk_level=risk_level,
        violations=violations,
        suggested_rewrite=selected_copy,
        human_review_required=report["human_review_required"],
        report_id=report["report_id"], created_at=report["created_at"],
        scope_notice="公开法规与模拟平台规则驱动的演示结果，不代表任何公司的内部审核结论。",
    )
    return payload.model_dump_json(indent=2)


_REVIEW_DEPS = ReviewDeps(
    scan_material=scan_material,
    retrieve_applicable_policy=retrieve_applicable_policy,
    search_similar_cases=search_similar_cases,
    generate_compliant_rewrite=generate_compliant_rewrite,
    add_finding=add_finding,
    get_policy_ids=lambda: {policy["id"] for policy in POLICIES},
    llm_agent_class=ReviewLLMAgent,
)


@st.cache_resource
def get_review_graph():
    return build_review_graph(_REVIEW_DEPS)


def run_review_agent(
    copy, landing, industry, product, audience, uploaded, qualification,
    brand_terms, use_llm=False,
):
    """Adapter over the LangGraph orchestration in ``review_graph``.

    Deterministic red-line controls and the LLM tool planner both live inside
    the compiled graph; this wrapper keeps the legacy call sites and return
    shape unchanged, translating the Streamlit upload into a plain byte size.
    """
    return run_review(
        get_review_graph(), _REVIEW_DEPS,
        copy=copy, landing=landing, industry=industry, product=product,
        audience=audience, image_size=(uploaded.size if uploaded else 0),
        qualification=qualification, brand_terms=brand_terms, use_llm=use_llm,
    )


def badge(text, tone="blue"):
    return f'<span class="badge {tone}">{html.escape(str(text))}</span>'


def render_finding(finding):
    policy = policy_by_id(finding["policy_id"])
    badge_color = "red" if finding["severity"] == "高" else "orange"
    with st.container(border=True, gap="small"):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.badge(finding["source"], color="gray")
            st.markdown(f"**{finding['category']}**")
            st.badge(f"{finding['severity']}风险", color=badge_color)
        st.markdown(f"证据：:red-background[**{html.escape(str(finding['evidence']))}**]")
        st.write(finding["reason"])
        st.caption(f"依据：{policy['id']} · {policy['title']} · {policy['source']}")


def create_ticket(report, copy_version, note):
    ticket_id = f"AR-{datetime.now().strftime('%m%d-%H%M%S')}"
    ticket = {
        "工单号": ticket_id,
        "广告主": "演示广告主",
        "行业": report["industry"],
        "商品": report["product"],
        "文案": report["copy"],
        "风险": "高" if report["risk_score"] >= 48 else "中",
        "优先级": "P0" if report["risk_score"] >= 70 else "P1",
        "状态": "待领取",
        "机器结论": report["decision"],
        "命中规则": ", ".join(sorted({finding["policy_id"] for finding in report["findings"]})),
        "提交时间": "刚刚",
        "建议文案": copy_version,
        "备注": note,
        "报告号": report["report_id"],
        "广告位": "信息流·推荐页",
        "素材类型": "单图+文案",
        "审核队列": f"{report['industry']}{'高风险' if report['risk_score'] >= 48 else '普通'}队列",
        "SLA截止": (datetime.now() + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M"),
        "落地页信息": report.get("landing_page_text", ""),
        "资质已核验": report.get("qualification_verified", False),
        "品牌禁用词": report.get("brand_terms", ""),
        "目标人群": report.get("audience", ""),
    }
    return persist_ticket(ticket)


def create_review_ticket(report, copy_version, note):
    return create_ticket(report, copy_version, note)


@st.cache_data(show_spinner=False, ttl="10m")
def run_evaluation_suite():
    cases = json.loads((BASE_DIR / "data" / "evaluation_cases.json").read_text(encoding="utf-8"))
    rows = []
    total_positive = true_positive = false_positive = 0
    total_negative = localized = localization_total = 0
    cited = citation_total = rewrite_residual = risky_rewrites = 0
    escalation_correct = complete_reports = 0
    required_fields = {
        "decision", "risk_level", "violations", "suggested_rewrite",
        "human_review_required", "report_id", "created_at", "scope_notice",
    }
    for case in cases:
        report = scan_material(
            case["copy"], "测试商品，具体信息以页面为准。", case["industry"],
            "测试商品", "大众人群", None, True, "",
        )
        predicted_risk = bool(report["findings"])
        expected_risk = case["expected_risk"]
        total_positive += int(expected_risk)
        total_negative += int(not expected_risk)
        true_positive += int(expected_risk and predicted_risk)
        false_positive += int(not expected_risk and predicted_risk)
        predicted_evidence = [str(item["evidence"]) for item in report["findings"]]
        for evidence in case["evidence"]:
            localization_total += 1
            localized += int(any(evidence in item or item in evidence for item in predicted_evidence))
        for finding in report["findings"]:
            citation_total += 1
            cited += int(bool(citations_for_findings([finding], limit=1)))
        escalation_correct += int(report["human_review_required"] == case["expected_escalate"])
        variants = generate_compliant_rewrite(case["copy"], "测试商品", "大众人群", report["findings"])
        rewritten = variants["最小修改版"]
        if expected_risk:
            risky_rewrites += 1
            rewrite_residual += int(bool(check_forbidden_words(rewritten, case["industry"])))
        payload = json.loads(report_json(report, rewritten))
        complete_reports += int(required_fields.issubset(payload) and all(payload[key] is not None for key in required_fields))
        rows.append({
            "案例": case["id"], "文案": case["copy"],
            "人工标签": "风险" if expected_risk else "合规",
            "系统结果": report["decision"], "风险项": len(report["findings"]),
            "命中证据": "、".join(predicted_evidence) or "无",
        })
    count = len(cases)
    metrics = {
        "违规召回率": true_positive / max(1, total_positive),
        "合规素材误报率": false_positive / max(1, total_negative),
        "风险片段定位率": localized / max(1, localization_total),
        "公开法规引用覆盖率": cited / max(1, citation_total),
        "改写残留规则率": rewrite_residual / max(1, risky_rewrites),
        "人工升级判断准确率": escalation_correct / max(1, count),
        "结构化字段完整率": complete_reports / max(1, count),
    }
    return {"metrics": metrics, "rows": rows, "count": count, "mode": "离线确定性规则评测"}


initialize_database(SEED_TICKETS)
EVIDENCE_CONFIG_ERRORS = validate_evidence_bindings({policy["id"] for policy in POLICIES})
init_state()

st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Noto+Sans+SC:wght@400;500;600;700&display=swap');
:root{--ink:#17212b;--muted:#687582;--line:#e2e7ed;--blue:#255bd7;--bg:#f5f7fa;--red:#c43f46;--amber:#9a6508;--green:#197447}
*{font-family:'DM Sans','Noto Sans SC',sans-serif;letter-spacing:0}.stApp{background:var(--bg);color:var(--ink)}.block-container{max-width:1480px;padding:1.5rem 2.4rem 3rem}
[data-testid="stSidebar"]{background:#101923;border-right:0}[data-testid="stSidebar"] *{color:#dfe7ef}.brand{display:flex;align-items:center;gap:12px;padding:3px 0 26px}.brand-mark{width:34px;height:34px;border-radius:7px;background:#2d65e8;display:grid;place-items:center;font-weight:700;color:white}.brand-name{font-weight:700}.brand-sub{font-size:11px;color:#8fa0b0;margin-top:2px}
h1{font-size:2rem!important;margin:.1rem 0 .25rem!important}h2{font-size:1.08rem!important;margin:0!important}.topline{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:1.25rem}.eyebrow{color:var(--blue);font-size:11px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase}.subcopy{color:var(--muted);font-size:13px;margin-top:4px}.status{background:#e8f5ee;color:var(--green);border:1px solid #c5e7d4;padding:7px 11px;border-radius:6px;font-size:12px;font-weight:600}
.panel{background:white;border:1px solid var(--line);border-radius:8px;padding:19px;box-shadow:0 2px 8px rgba(21,31,43,.025)}.panel-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:15px}.panel-kicker{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:1px;font-weight:700}.badge{display:inline-block;padding:3px 8px;border-radius:99px;font-size:10px;font-weight:700;margin-left:8px}.badge.high{background:#fff0f0;color:var(--red)}.badge.medium{background:#fff5dd;color:var(--amber)}.badge.blue{background:#edf2ff;color:var(--blue)}.badge.green{background:#e8f5ee;color:var(--green)}
.metric{border:1px solid var(--line);border-radius:7px;padding:12px 14px;background:#fbfcfe}.metric-label{color:var(--muted);font-size:11px}.metric-value{font-size:20px;font-weight:700;margin-top:3px}.metric-note{font-size:10px;color:#8b97a3;margin-top:2px}.finding{border-top:1px solid var(--line);padding:15px 0 13px}.finding:first-of-type{border-top:0}.finding-top{display:flex;justify-content:space-between;align-items:center;font-size:13px}.source-dot{font-size:9px;background:#eef1f5;color:#667380;padding:3px 6px;border-radius:4px;margin-right:8px}.evidence{font-size:13px;margin:9px 0 6px;color:#283746;font-weight:600}.muted{font-size:12px;color:var(--muted);line-height:1.55}.policy-ref{color:#5270a8;font-size:10px;margin-top:9px}
.copy-preview{background:#fbfcfe;border:1px solid var(--line);border-radius:7px;padding:15px;font-size:15px;line-height:1.8}.copy-preview mark{background:#ffe1e1;color:#a92c35;border-bottom:2px solid #db5e67;padding:1px 2px}.recommend{background:#f1f5ff;border:1px solid #d8e2ff;border-radius:7px;padding:14px;font-size:14px;line-height:1.65;color:#1f3974;min-height:75px}.reason{border-left:3px solid #6b8eea;padding-left:11px;color:#596778;font-size:11px;line-height:1.6}.step{display:flex;gap:10px;padding:8px 0}.step-num{width:22px;height:22px;border-radius:50%;background:#e9efff;color:#2858c5;display:grid;place-items:center;font-size:10px;font-weight:700}.step-title{font-size:12px;font-weight:600}.step-sub{font-size:10px;color:var(--muted)}
.policy-card{border:1px solid var(--line);border-radius:7px;padding:15px;background:white;height:100%}.policy-id{font-size:10px;color:var(--blue);font-weight:700}.policy-title{font-size:14px;font-weight:700;margin:5px 0}.policy-text{font-size:11px;color:var(--muted);line-height:1.6}.case{border-bottom:1px solid #263440;padding:10px 0}.case-title{font-size:11px;line-height:1.4}.case-tag{color:#95a8ba;font-size:10px;margin-top:4px}.section-title{font-size:16px;font-weight:700;margin:10px 0 14px}.queue-row{display:grid;grid-template-columns:1.1fr 1fr 1.5fr .7fr .9fr 1fr;gap:12px;padding:13px 12px;border-bottom:1px solid var(--line);align-items:center;font-size:12px;background:white}.queue-head{font-size:10px;color:var(--muted);font-weight:700;background:#f6f8fb}.trace{display:flex;flex-wrap:wrap;gap:6px}.trace span{font-size:10px;color:#536273;background:#eef1f5;padding:5px 7px;border-radius:4px}
.stButton>button,.stDownloadButton>button{border-radius:6px;font-weight:600}.stTabs [data-baseweb="tab-list"]{gap:20px}.stTabs [data-baseweb="tab"]{padding-left:2px;padding-right:2px}footer{visibility:hidden}
@media(max-width:900px){.block-container{padding:1rem}.topline{display:block}.status{display:inline-block;margin-top:12px}.queue-row{grid-template-columns:1fr 1fr}.queue-head{display:none}}
</style>""", unsafe_allow_html=True)

# Readability overrides. Keep these last so they win over legacy styles above.
st.markdown("""<style>
:root{--ink:#17212b;--muted:#5f6f7f;--line:#d9e0e8;--blue:#1858d5;--bg:#f4f7fb}
html,body,.stApp{font-family:"Microsoft YaHei UI","PingFang SC","Segoe UI",sans-serif;letter-spacing:0}
.stApp{background:var(--bg)!important;color:var(--ink)!important}.block-container{max-width:1320px;padding:4.75rem 2.2rem 3rem}
[data-testid="stSidebar"]{background:#fff!important;border-right:1px solid var(--line)!important}[data-testid="stSidebar"] *{color:#27384a!important}
[data-testid="stSidebar"] [role="radiogroup"] label{padding:.42rem .55rem;border-radius:6px;margin:.12rem 0}
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked){background:#eaf1ff!important;color:#174da8!important}
.brand{padding:4px 0 24px}.brand-mark{width:38px;height:38px;background:#1858d5}.brand-name{font-size:15px;color:#17212b!important}.brand-sub{font-size:12px;color:#66798c!important}
h1{font-size:1.85rem!important;line-height:1.25!important;margin:.15rem 0 .4rem!important;color:#15202b!important}h2,h3{color:#1d2a36!important}
[data-testid="stWidgetLabel"] p{font-size:14px!important;font-weight:600!important;color:#2b3b4b!important}
[data-testid="stCaptionContainer"] p{font-size:12px!important;color:#657688!important;line-height:1.55!important}
[data-testid="stVerticalBlockBorderWrapper"]{background:#fff!important;border-color:var(--line)!important;border-radius:8px!important;box-shadow:0 1px 2px rgba(16,24,40,.03)}
.page-kicker{font-size:11px;color:#1858d5;font-weight:700;letter-spacing:1px;text-transform:uppercase}.page-subtitle{font-size:14px;color:#607183;line-height:1.6;margin-bottom:8px}
.copy-preview{background:#f8fafc;border:1px solid var(--line);border-radius:6px;padding:16px;font-size:15px;line-height:1.9;color:#263646}.copy-preview mark{background:#ffe0e2;color:#9d2731;border-bottom:2px solid #d95560;padding:1px 3px}
.recommend{background:#eef4ff;border:1px solid #cddcff;border-radius:6px;padding:16px;font-size:15px;line-height:1.8;color:#173d78;min-height:86px}.reason{border-left:3px solid #3572dc;padding:3px 0 3px 12px;color:#526579;font-size:13px;line-height:1.65}
.stButton>button,.stDownloadButton>button{border-radius:6px;font-weight:600;min-height:40px}.stTabs [data-baseweb="tab"]{font-size:14px;font-weight:600}.stTabs [data-baseweb="tab-list"]{gap:24px}
footer{visibility:hidden}@media(max-width:900px){.block-container{padding:4.25rem 1rem 2rem}.brand-sub{display:none}}
</style>""", unsafe_allow_html=True)


with st.sidebar:
    st.markdown('<div class="brand"><div class="brand-mark">C</div><div><div class="brand-name">Creative Compliance Copilot</div><div class="brand-sub">广告素材智能预审与审核员辅助系统</div></div></div>', unsafe_allow_html=True)
    if st.session_state.current_user is None:
        st.badge("审核员未登录", icon=":material/lock:", color="gray")
    else:
        current_user = st.session_state.current_user
        st.markdown(f"**{current_user['name']}**")
        st.caption(f"{current_user['role']} · {current_user['team']}\n\n{current_user['auth_source']}")
        page = st.radio("主导航", ["审核工作台", "机器预审", "规则中心", "质检评测"], label_visibility="collapsed")
        if st.button("退出登录", icon=":material/logout:", width="stretch"):
            clear_reviewer_session()
            st.rerun()
        st.space("medium")
        st.caption("系统状态")
        st.badge("规则引擎在线", icon=":material/check_circle:", color="green")
        if ReviewLLMAgent().available:
            st.badge("LLM Agent 已配置", icon=":material/hub:", color="blue")
        else:
            st.badge("离线确定性模式", icon=":material/settings:", color="gray")
        st.caption(f"{len(POLICIES)} 条执行规则\n\n{len(SOURCES)} 份官方法规原文\n\n版本 2026.07 · 中国大陆")
        if EVIDENCE_CONFIG_ERRORS:
            st.error("政策证据映射配置异常，请联系规则管理员。")


def page_header(kicker, title, subtitle):
    st.markdown(f'<div class="page-kicker">{kicker}</div>', unsafe_allow_html=True)
    st.title(title)
    st.caption("AI-assisted Ad Review and Policy Retrieval System")
    st.markdown(f'<div class="page-subtitle">{subtitle}</div>', unsafe_allow_html=True)
    st.space("small")


def sla_state(ticket, now=None):
    if ticket.get("状态") in {"已通过", "已驳回", "要求整改", "升级复核"}:
        return "已结案"
    deadline_text = ticket.get("SLA截止", "")
    if not deadline_text:
        return "未配置"
    try:
        deadline = datetime.strptime(deadline_text, "%Y-%m-%d %H:%M")
    except ValueError:
        return "格式异常"
    seconds = (deadline - (now or datetime.now())).total_seconds()
    if seconds < 0:
        return "已超时"
    if seconds <= 3600:
        return "即将超时"
    return "正常"


if st.session_state.current_user is None:
    page_header("IDENTITY ACCESS", "审核员身份验证", "Creative Compliance Copilot 审核工作区")
    login_left, login_center, login_right = st.columns([1, 1.15, 1])
    with login_center:
        with st.container(border=True):
            st.subheader("登录审核工作台")
            with st.form("reviewer_login", border=False):
                login_account = st.text_input("员工账号", "reviewer.a")
                login_password = st.text_input("访问口令", "demo123", type="password")
                login_submit = st.form_submit_button("登录", icon=":material/login:", type="primary", width="stretch")
            if login_submit:
                authenticated_user = authenticate_demo_reviewer(login_account, login_password)
                if authenticated_user:
                    st.session_state.current_user = authenticated_user
                    st.session_state.pop("login_error", None)
                    st.rerun()
                else:
                    st.session_state.login_error = "账号或访问口令错误"
            if st.session_state.get("login_error"):
                st.error(st.session_state.login_error)
            st.caption("演示身份认证 · 生产部署需接入企业 SSO/OIDC")
    st.stop()


if page == "机器预审":
    page_header("AI PRE-REVIEW / 02", "机器预审台", "审核员可在这里导入待审素材，复核规则检查、政策检索和整改建议。")
    input_col, result_col = st.columns([1, 1.35], gap="large")
    with input_col:
        with st.container(border=True):
            st.subheader("1. 提交广告素材")
            st.caption("填写素材和落地页关键信息。系统会检查文案、资质、品牌规则及页面一致性。")
            with st.form("review_form", border=False):
                industry = st.selectbox("广告品类", ALL_INDUSTRIES, help="选择品类后仅运行通用规则和该品类的专项规则")
                product = st.text_input("商品名称", "透亮修护精华")
                copy = st.text_area("广告文案", DEFAULT_COPY, height=120)
                landing = st.text_area("落地页关键信息", DEFAULT_LANDING, height=105, help="填写价格、商品名、核心卖点和限制条件")
                audience = st.selectbox("目标人群", ["18-35 岁女性", "学生群体", "职场人群", "大众人群"])
                objective = st.selectbox("投放目标", ["提升点击率", "促进转化", "品牌曝光", "线索收集"])
                uploaded = st.file_uploader("图片素材（可选）", type=["png", "jpg", "jpeg"])
                qualification = st.checkbox("已上传并核验相关资质或证明材料", value=False)
                brand_terms = st.text_input("企业品牌禁用词", "闭眼入，黄脸婆，不买就亏")
                use_llm = st.checkbox(
                    "使用 LLM Agent 增强分析",
                    value=False,
                    disabled=not ReviewLLMAgent().available,
                    help="仅在提交本次预审时调用模型；未勾选时运行快速确定性流程。",
                )
                run = st.form_submit_button("开始预审", icon=":material/play_arrow:", type="primary", width="stretch")
            ocr_text, ocr_confidence = "", 0.0
            image_error = None
            if uploaded:
                try:
                    validate_image_bytes(uploaded.getvalue())
                    st.image(uploaded, width="stretch")
                    with st.spinner("正在执行本地 OCR…"):
                        ocr_text, ocr_confidence = extract_text_from_image(uploaded.getvalue())
                        visual_result = analyze_image_visual(uploaded.getvalue())
                    if ocr_text:
                        st.text_area("OCR 提取文字", ocr_text, height=90, disabled=True)
                        st.caption(f"RapidOCR · 平均置信度 {ocr_confidence:.0%} · {uploaded.size / 1024:.0f} KB")
                    else:
                        st.warning("图片中未识别到清晰文字，请由审核员检查原图。")
                    st.caption(f"视觉检查：{visual_result['width']}×{visual_result['height']} px · 平均亮度 {visual_result['brightness']}/255")
                    for visual_warning in visual_result["warnings"]:
                        st.warning(visual_warning, icon=":material/image_search:")
                except (ValueError, RuntimeError) as error:
                    image_error = str(error)
                    st.error(image_error, icon=":material/broken_image:")

        with st.expander("系统会执行哪些检查？", icon=":material/account_tree:"):
            st.markdown("""
1. **素材解析**：读取文案、图片文字与基本规格  
2. **规则筛选**：按行业选择适用法规和平台规则  
3. **确定性检查**：检查禁用词、资质、价格与格式  
4. **一致性检查**：比对广告素材和落地页  
5. **合规改写**：生成不增加虚假卖点的替代版本
""")

    if run and image_error is None:
        combined_copy = f"{copy}\n图片文字：{ocr_text}" if ocr_text else copy
        with st.spinner("正在运行预审流程…"):
            workflow_result = run_review_agent(
                combined_copy, landing, industry, product, audience, uploaded,
                qualification, brand_terms, use_llm=use_llm,
            )
        st.session_state.current_report = workflow_result["report"]
        st.session_state.current_workflow = workflow_result
        st.session_state.ocr_text = ocr_text
        st.session_state.scan_count += 1
        st.session_state.pop("created_ticket", None)
    elif run and image_error:
        st.warning("图片校验未通过，本次预审未执行。请更换素材后重试。")

    with result_col:
        report = st.session_state.current_report
        if report is None:
            report = scan_material(copy, landing, industry, product, audience, uploaded.size if uploaded else 0, qualification, brand_terms)
            st.session_state.current_report = report
        findings = report["findings"]
        variants = generate_compliant_rewrite(report["copy"], report["product"], report["audience"], findings)
        report_similar_cases = search_similar_cases(report["copy"], industry=report["industry"], limit=3)
        report_rag_results = retrieve_applicable_policy(report["copy"], findings, limit=3)
        decision_color = "red" if report["decision"] == "人工审核" else ("orange" if report["decision"] == "修改后提交" else "green")

        with st.container(border=True):
            with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
                st.subheader("2. 预审结果")
                st.badge(report["decision"], icon=":material/warning:" if findings else ":material/check_circle:", color=decision_color)
            st.caption(f"报告 {report['report_id']} · {report['created_at']} · 行业：{report['industry']}")
            st.caption(f"执行模式：{report.get('execution_mode', '离线确定性流程')} · 行业来源：{report.get('industry_source', '人工选择')}")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("风险分", f'{report["risk_score"]}/100')
            m2.metric("风险项", len(findings))
            m3.metric("高风险", sum(f["severity"] == "高" for f in findings))
            m4.metric("人工复核", "需要" if report["human_review_required"] else "暂不")

            tab1, tab2, tab3, tab4 = st.tabs([f"风险清单（{len(findings)}）", "原文定位", "引用依据", "相似案例"])
            with tab1:
                if findings:
                    st.caption("按风险逐项修改；高风险项目应在提交投放前完成证据核验或人工复核。")
                    for finding in findings:
                        render_finding(finding)
                else:
                    st.success("未发现明显风险，可以进入业务确认。", icon=":material/check_circle:")
            with tab2:
                st.caption("红色标记为文案中的直接命中证据；价格、资质等跨字段问题在风险清单中展示。")
                st.markdown("**广告文案**")
                st.markdown(f'<div class="copy-preview">{highlight_copy(report["copy"], findings)}</div>', unsafe_allow_html=True)
                st.markdown("**落地页信息**")
                st.markdown(f'<div class="copy-preview">{html.escape(report["landing_page_text"])}</div>', unsafe_allow_html=True)
            with tab3:
                cited = sorted({f["policy_id"] for f in findings})
                if cited:
                    for pid in cited:
                        p = policy_by_id(pid)
                        with st.expander(f"{p['id']} · {p['title']}", icon=":material/gavel:"):
                            st.write(p["text"])
                            st.caption(f"来源：{p['source']} · 类型：{p.get('basis', '未标注')} · 适用行业：{p['industry']}")
                else:
                    st.info("当前未命中需要引用的风险规则。")
                if report_rag_results:
                    st.caption("规则绑定的公开法规片段（确定性引用）")
                    for result in report_rag_results:
                        with st.expander(f"{result['id']} · {result['title']}"):
                            st.write(result["excerpt"])
                            st.caption(f"{result['interpretation']} · 不计算相似度")
                unsupported = unsupported_rule_ids(findings)
                if unsupported:
                    st.warning(f"未找到可直接引用的公开法规片段：{', '.join(unsupported)}。这些规则仅作为模拟平台/企业规则展示，需人工核验。")
            with tab4:
                if not report_similar_cases:
                    st.info("未找到达到相似度门槛的同品类历史案例。")
                else:
                    for case in report_similar_cases:
                        with st.container(border=True):
                            st.markdown(f"**{case['case_id']} · {case['decision']}**")
                            st.write(case["copy"])
                            st.caption(f"{case['category']} · 相似度 {case['score']:.0%} · {case['reason']}")

        st.space("small")
        rewrite_col, action_col = st.columns([1.15, .85], gap="medium")
        with rewrite_col:
            with st.container(border=True):
                st.subheader("3. 选择合规改写")
                selected_name = st.segmented_control("改写风格", list(variants.keys()), default=st.session_state.selected_variant, width="stretch") or "最小修改版"
                st.session_state.selected_variant = selected_name
                selected_copy = variants[selected_name]
                st.markdown(f'<div class="recommend">{html.escape(selected_copy)}</div>', unsafe_allow_html=True)
                st.markdown('<div class="reason"><b>改写边界</b><br>删除效果保证和绝对化范围；保留真实商品信息；不新增权威背书、成分或认证。</div>', unsafe_allow_html=True)

        with action_col:
            with st.container(border=True):
                st.subheader("4. 提交人工复核")
                st.caption("模型只提供预审建议，不自动封禁素材。")
                note = st.text_input("审核备注", "请复核功效宣称与落地页价格")
                ticket_confirmation = st.segmented_control("创建确认", ["未确认", "已确认"], default="未确认", key="ticket_confirmation", width="stretch")
                if st.button("创建审核工单", icon=":material/assignment_add:", width="stretch", type="primary", disabled=not findings or ticket_confirmation != "已确认"):
                    st.session_state.created_ticket = create_review_ticket(report, selected_copy, note)
                report_data = report_json(report, selected_copy)
                st.download_button("下载风险报告", report_data, file_name=f"{report['report_id']}.json", mime="application/json", icon=":material/download:", width="stretch")
                if st.session_state.get("created_ticket"):
                    st.success(f"工单 {st.session_state.created_ticket} 已创建", icon=":material/check_circle:")


elif page == "审核工作台":
    page_header("REVIEW OPERATIONS / 01", "广告审核工作台", "面向平台审核员的智能预审与裁决辅助。AI 提供证据，最终结论由审核员确认。")
    persisted_tickets = list_tickets()
    now = datetime.now()
    active_tickets = [ticket for ticket in persisted_tickets if ticket.get("状态") in {"待领取", "复核中"}]
    sla_alerts = sum(sla_state(ticket, now) in {"已超时", "即将超时"} for ticket in active_tickets)
    closed_today = sum(ticket.get("状态") in {"已通过", "已驳回", "要求整改", "升级复核"} for ticket in persisted_tickets)
    q1, q2, q3, q4 = st.columns(4)
    queue_metrics = [
        ("待处理", len(active_tickets)),
        ("SLA 预警", sla_alerts),
        ("高风险", sum(t.get("风险") == "高" for t in persisted_tickets)),
        ("已结案", closed_today),
    ]
    for col, (label, value) in zip([q1, q2, q3, q4], queue_metrics):
        col.metric(label, value, help="审核 SLA：4 小时")
    st.space("small")
    selected_ticket = None
    with st.expander("审核任务库", expanded=False, icon=":material/list_alt:"):
        with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
            st.subheader("任务筛选与选择")
            st.caption(f"共 {len(persisted_tickets)} 条模拟任务 · 数据更新时间 {now.strftime('%H:%M')}")
        industry_col, filter_col, priority_col, queue_col, search_col = st.columns([1, 1, 1, 1.4, 2])
        with industry_col:
            industry_filter = st.selectbox("品类筛选", ["全部", *ALL_INDUSTRIES])
        with filter_col:
            status_filter = st.selectbox("状态筛选", ["全部", "待领取", "复核中", "要求整改", "已驳回", "已通过", "升级复核"])
        with priority_col:
            priority_filter = st.selectbox("优先级", ["全部", "P0", "P1", "P2"])
        with queue_col:
            queue_options = ["全部", *sorted({ticket.get("审核队列", "未分队") for ticket in persisted_tickets})]
            queue_filter = st.selectbox("审核队列", queue_options)
        with search_col:
            ticket_search = st.text_input("搜索任务", placeholder="任务号、广告主、商品、文案或规则 ID")
        tickets = [
            ticket for ticket in persisted_tickets
            if (industry_filter == "全部" or ticket.get("行业") == industry_filter)
            and (status_filter == "全部" or ticket.get("状态") == status_filter)
            and (priority_filter == "全部" or ticket.get("优先级") == priority_filter)
            and (queue_filter == "全部" or ticket.get("审核队列") == queue_filter)
            and (
                not ticket_search or ticket_search.lower() in " ".join([
                    ticket.get("工单号", ""), ticket.get("广告主", ""),
                    ticket.get("行业", ""), ticket.get("商品", ""), ticket.get("文案", ""),
                    ticket.get("命中规则", ""),
                ]).lower()
            )
        ]
        if tickets:
            visible_columns = ["工单号", "行业", "优先级", "风险", "状态", "SLA状态", "广告主", "商品", "审核队列", "广告位", "提交时间"]
            task_rows = []
            for ticket in tickets:
                row = {key: ticket.get(key, "") for key in visible_columns}
                row["SLA状态"] = sla_state(ticket, now)
                task_rows.append(row)
            st.caption(f"当前筛选显示 {len(tickets)} 条；可点击列头排序。")
            task_selection = st.dataframe(
                task_rows, width="stretch", hide_index=True, height=420,
                on_select="rerun", selection_mode="single-row", key="task_library_selection",
                column_config={
                    "工单号": st.column_config.TextColumn("工单号", pinned=True),
                    "优先级": st.column_config.TextColumn("优先级", width="small"),
                    "风险": st.column_config.TextColumn("风险", width="small"),
                    "状态": st.column_config.TextColumn("状态", width="small"),
                    "SLA状态": st.column_config.TextColumn("SLA", width="small"),
                },
            )
            if task_selection.selection.rows:
                selected_index = task_selection.selection.rows[0]
                if 0 <= selected_index < len(tickets):
                    selected_ticket = tickets[selected_index]
                    st.session_state.selected_ticket_id = selected_ticket["工单号"]
        else:
            st.info("没有符合筛选条件的工单。")
    if selected_ticket is None and tickets:
        selected_ticket = next(
            (ticket for ticket in tickets if ticket["工单号"] == st.session_state.selected_ticket_id),
            next((ticket for ticket in tickets if ticket.get("状态") in REVIEWABLE_STATUSES), tickets[0]),
        )
        st.session_state.selected_ticket_id = selected_ticket["工单号"]
    if selected_ticket:
        chosen = selected_ticket["工单号"]
        st.caption(f"当前任务：{chosen} · {selected_ticket.get('广告主', '')} · {selected_ticket.get('商品', '')}")
        cached_workflows = st.session_state.setdefault("workbench_workflows", {})
        run_llm_review = st.button(
            "运行 LLM 增强分析",
            icon=":material/hub:",
            disabled=not ReviewLLMAgent().available,
            help="仅在点击后调用模型；筛选、选择和人工确认不会触发模型请求。",
        )
        review_inputs = {
            "copy": selected_ticket.get("文案", DEFAULT_COPY),
            "landing": selected_ticket.get("落地页信息", ""),
            "industry": selected_ticket.get("行业", "护肤品"),
            "product": selected_ticket.get("商品", "待审商品"),
            "audience": selected_ticket.get("目标人群") or "大众人群",
            "uploaded": None,
            "qualification": selected_ticket.get("资质已核验", False),
            "brand_terms": selected_ticket.get("品牌禁用词", ""),
        }
        if run_llm_review:
            with st.spinner("正在运行 LLM 增强分析…"):
                cached_workflows[chosen] = run_review_agent(
                    **review_inputs, use_llm=True,
                )
        selected_workflow = cached_workflows.get(chosen)
        if selected_workflow is None:
            selected_workflow = run_review_agent(**review_inputs)
        selected_report = selected_workflow["report"]
        selected_findings = selected_report["findings"]
        rag_results = selected_workflow["rag_results"]
        similar_cases = selected_workflow["similar_cases"]

        material_col, analysis_col, decision_col = st.columns([0.9, 1.15, 0.95], gap="medium")
        with material_col:
            with st.container(border=True):
                st.subheader("广告主提交素材")
                with st.container(horizontal=True):
                    st.badge(selected_ticket.get("优先级", "P1"), color="red" if selected_ticket.get("优先级") == "P0" else "orange")
                    st.badge(selected_ticket.get("状态", "待领取"), color="blue")
                    sla_label = sla_state(selected_ticket)
                    st.badge(sla_label, color="red" if sla_label in {"已超时", "即将超时"} else "gray")
                st.markdown(f"**广告主：** {selected_ticket.get('广告主', '未登记')}")
                st.markdown(f"**商品：** {selected_ticket.get('商品', '')}")
                st.caption(f"{selected_ticket.get('审核队列', '美妆普通队列')} · {selected_ticket.get('广告位', '未配置广告位')} · {selected_ticket.get('素材类型', '图文')}")
                st.caption(f"提交：{selected_ticket.get('提交时间', '未知')} · SLA：{selected_ticket.get('SLA截止', '未配置')} · 当前审核员：{selected_ticket.get('审核员') or '未分配'}")
                st.markdown("**广告文案**")
                st.markdown(f'<div class="copy-preview">{html.escape(selected_ticket.get("文案", ""))}</div>', unsafe_allow_html=True)
                st.caption("单张图片素材：当前演示任务未附图。可在“机器预审”页上传图片并创建任务。")
                st.markdown(f"**机器结论：** {selected_ticket.get('机器结论', selected_report['decision'])}")
                st.caption(f"命中规则：{selected_ticket.get('命中规则', '待计算')}")

        with analysis_col:
            with st.container(border=True):
                st.subheader("AI 风险分析")
                visible_steps = [step for step in selected_workflow["trace"] if step["status"] == "completed"]
                with st.status(f"审核流程已完成 · {len(visible_steps)} 个步骤", state="complete", expanded=False):
                    for step in visible_steps:
                        step_name = TOOL_DISPLAY_NAMES.get(step["tool"], "审核处理")
                        st.write(f"**{step_name}** · {step['summary']}")
                st.markdown(f'<div class="copy-preview">{highlight_copy(selected_ticket.get("文案", ""), selected_findings)}</div>', unsafe_allow_html=True)
                st.caption("仅展示工具调用结果和风险证据，不展示模型隐藏推理过程。")
                for finding in selected_findings[:5]:
                    render_finding(finding)

        with decision_col:
            with st.container(border=True):
                st.subheader("政策依据")
                cited_ids = list(dict.fromkeys(finding["policy_id"] for finding in selected_findings))[:3]
                if cited_ids:
                    for policy_id in cited_ids:
                        policy = policy_by_id(policy_id)
                        with st.expander(f"{policy_id} · {policy['title']}"):
                            st.write(policy["text"])
                            st.caption(policy["source"])
                else:
                    st.warning("RAG 未找到直接支持该风险的规则，不得生成引用；请人工检索。")
                if rag_results:
                    st.caption("公开法规确定性引用")
                    for result in rag_results:
                        with st.expander(f"{result['id']} · {result['title']}"):
                            st.write(result["excerpt"])
                            st.caption(f"{result['interpretation']} · 引用方式：规则绑定，不计算相似度")
                unsupported = unsupported_rule_ids(selected_findings)
                if unsupported:
                    st.warning(f"以下模拟规则尚未绑定公开法规片段：{', '.join(unsupported)}。请人工检索，不生成依据。")

                st.subheader("相似历史案例")
                if not similar_cases:
                    st.info("未找到达到相似度门槛的同品类历史案例。")
                else:
                    for case in similar_cases:
                        with st.expander(f"{case['case_id']} · {case['decision']}"):
                            st.write(case["copy"])
                            st.caption(f"{case['category']} · 相似度 {case['score']:.0%} · {case['reason']}")

                st.subheader("人工审核结论")
                reviewer = st.session_state.current_user["name"]
                st.caption(f"当前审核员：{reviewer} · {st.session_state.current_user['role']} · 已登录")
                is_reviewable = selected_ticket.get("状态") in REVIEWABLE_STATUSES
                if not is_reviewable:
                    st.info(
                        f"工单状态为“{selected_ticket.get('状态', '未知')}”，已结案记录仅供回放，不能再次裁决。",
                        icon=":material/lock:",
                    )
                review_note = st.text_area(
                    "审核意见", value="", placeholder="填写判断依据、整改项或升级原因",
                    key=f"human_review_note_{chosen}",
                    disabled=not is_reviewable,
                )
                confirmation_state = st.segmented_control(
                    "人工确认",
                    ["未确认", "已确认"],
                    default="未确认",
                    help="保存审核结论前，审核员必须确认已核验素材、政策依据和机器建议。",
                    key=f"human_confirm_state_{chosen}",
                    width="stretch",
                    disabled=not is_reviewable,
                )
                confirmed = is_reviewable and confirmation_state == "已确认"
                action_rows = [
                    [("通过", "已通过"), ("要求整改", "要求整改")],
                    [("驳回", "已驳回"), ("升级复核", "升级复核")],
                ]
                for row_index, row in enumerate(action_rows):
                    cols = st.columns(2)
                    for col, (label, status) in zip(cols, row):
                        with col:
                            if st.button(label, key=f"decision_{chosen}_{row_index}_{status}", width="stretch", disabled=not confirmed, type="primary" if label == "通过" else "secondary"):
                                saved_at = save_human_decision(
                                    chosen, reviewer, status, review_note,
                                    selected_ticket.get("文案", ""), selected_ticket.get("机器结论", ""),
                                    selected_report["report_id"],
                                )
                                st.session_state.last_saved_decision = f"{chosen} · {status} · {saved_at}"
                                st.rerun()
                if st.session_state.get("last_saved_decision"):
                    st.success(f"已保存：{st.session_state.last_saved_decision}")

        with st.expander("审核日志回放", icon=":material/history:"):
            logs = list_review_logs()
            if logs:
                st.dataframe(logs, width="stretch", hide_index=True)
            else:
                st.caption("暂无人工审核记录。完成一次审核后会在此显示。")


elif page == "规则中心":
    page_header("POLICY KNOWLEDGE / 03", "规则与政策中心", "检索多品类审核规则和公开法规原文，风险结论只引用当前适用版本。")
    with st.container(border=True):
        st.subheader("执行规则")
        with st.form("rule_filters", border=False):
            s1, s2, s3 = st.columns([2, 1, 1])
            with s1:
                query = st.text_input("检索规则", placeholder="输入规则名称、ID 或关键词")
            with s2:
                industry_filter = st.selectbox("适用品类", ["全部", "通用", *ALL_INDUSTRIES])
            with s3:
                level_filter = st.selectbox("风险等级", ["全部", "高", "中", "低"])
            st.form_submit_button("应用筛选", icon=":material/search:")
    filtered = [p for p in POLICIES if (not query or query.lower() in (p["title"] + p["text"] + p["id"]).lower()) and (industry_filter == "全部" or p["industry"] == industry_filter) and (level_filter == "全部" or p["level"] == level_filter)]
    page_size = 10
    total_pages = max(1, (len(filtered) + page_size - 1) // page_size)
    page_number = st.selectbox("结果页", range(1, total_pages + 1), format_func=lambda value: f"第 {value} / {total_pages} 页")
    start = (page_number - 1) * page_size
    st.caption(f"找到 {len(filtered)} 条规则，本页显示 {start + 1 if filtered else 0}–{min(start + page_size, len(filtered))} 条。法规基线来自公开文件；模拟规则需由企业合规负责人确认。")
    for policy in filtered[start:start + page_size]:
        with st.expander(f"{policy['id']}　{policy['title']}", icon=":material/rule:"):
            badge_color = "red" if policy["level"] == "高" else ("orange" if policy["level"] == "中" else "blue")
            with st.container(horizontal=True):
                st.badge(f"{policy['level']}风险", color=badge_color)
                st.badge(policy["industry"], color="gray")
                st.badge(policy.get("basis", "未标注"), color="blue")
            st.write(policy["text"])
            st.caption(f"依据：{policy['source']} · 生效：{policy['effective']}")
            if policy.get("action"):
                st.markdown(f"**建议动作：** {policy['action']}")

    with st.expander("检索法规原文", icon=":material/search:", expanded=False):
        legal_query = st.text_input("搜索条款关键词", placeholder="例如：绝对化用语、食品功效、宠物饲料、产品安全")
        if legal_query:
            excerpt_hits = search_policy_query(legal_query, limit=8)
            q = legal_query.lower()
            legal_hits = [chunk for chunk in LEGAL_INDEX if q in chunk["text"].lower()][:8]
            st.caption(f"命中 {len(excerpt_hits) + len(legal_hits)} 个条款/原文片段")
            for item in excerpt_hits:
                source = next((source for source in SOURCES if source["id"] == item["source_id"]), None)
                st.markdown(f'**{item["title"]} · {item["article"]}**')
                st.write(item["excerpt"])
                st.caption(f'BM25 {item["score"]:.2f} · {item["interpretation"]} · [官方页面]({source["url"] if source else "https://www.gov.cn/"})')
            for chunk in legal_hits:
                st.markdown(f'**{chunk["title"]} · {chunk["chunk_id"]}**')
                st.write(chunk["text"][:1100])
                st.caption(f'来源：{chunk["authority"]} · 抓取：{chunk["retrieved_at"]} · [官方页面]({chunk["url"]})')
            if not excerpt_hits and not legal_hits:
                st.warning("未找到达到阈值的可引用依据。请调整关键词或由政策人员人工检索，不生成弱相关引用。")
        else:
            st.caption(f"已建立 {len(LEGAL_INDEX)} 个法规原文片段和 {len(LEGAL_EXCERPTS)} 个条款级摘要索引。输入关键词后检索本地归档内容。")
    st.space("small")
    st.subheader("已归档官方法规")
    st.caption("这些文件已保存到项目，可打开官方页面核验，也可下载本地归档。")
    source_cols = st.columns(2)
    for index, basis in enumerate(SOURCES):
        with source_cols[index % 2]:
            with st.container(border=True):
                local_path = BASE_DIR / "data" / basis["local_file"]
                archive_status = f"{local_path.stat().st_size / 1024:.0f} KB" if local_path.exists() else "待同步"
                st.markdown(f"**{basis['title']}**")
                st.caption(f"{basis['authority']} · {basis['source_type']} · {archive_status} · {basis['retrieved_at']}")
                st.link_button("打开官方页面", basis["url"], icon=":material/open_in_new:", width="stretch")
                if local_path.exists():
                    mime = "application/pdf" if local_path.suffix.lower() == ".pdf" else "text/html"
                    st.download_button("下载本地归档", local_path.read_bytes(), file_name=local_path.name, mime=mime, icon=":material/download:", width="stretch", key=f"download_{basis['id']}")
    with st.expander("新增企业品牌规则（演示）", icon=":material/add:"):
        r1, r2 = st.columns(2)
        with r1:
            st.text_input("规则名称", "禁止制造容貌焦虑")
            st.selectbox("适用范围", ["全部品牌", "护肤品牌 A", "食品品牌 B", "宠物品牌 C", "数码品牌 D", "日用品牌 E"])
        with r2:
            st.text_area("规则内容", "不得使用贬低用户外貌、年龄或身材的表达。", height=100)
        st.button("保存为草稿", disabled=True, help="Demo 中仅展示权限边界，规则发布需要管理员审批")


else:
    page_header("EVALUATION / 04", "预审效果看板", "逐条运行固定测试集，指标来自当前代码的实际预测结果。")
    evaluation_result = run_evaluation_suite()
    evaluation = list(evaluation_result["metrics"].items())
    st.caption(f"运行模式：{evaluation_result['mode']} · 样本：{evaluation_result['count']} 条 · 指标缓存 10 分钟")
    metric_cols = st.columns(3)
    for index, (label, value) in enumerate(evaluation):
        metric_cols[index % 3].metric(label, f"{value:.1%}")
    st.space("small")
    chart_col, matrix_col = st.columns([1.15, .85], gap="large")
    with chart_col:
        with st.container(border=True):
            st.subheader("当前版本实际指标")
            chart_data = [
                {"指标": label, "百分比": round(value * 100, 1)}
                for label, value in evaluation
            ]
            chart = alt.Chart(alt.Data(values=chart_data)).mark_bar().encode(
                y=alt.Y("指标:N", sort=None, title=None, axis=alt.Axis(labelLimit=230)),
                x=alt.X("百分比:Q", title="百分比", scale=alt.Scale(domain=[0, 100])),
                tooltip=[alt.Tooltip("指标:N", title="指标"), alt.Tooltip("百分比:Q", title="百分比", format=".1f")],
            ).properties(height=240)
            labels = chart.mark_text(align="left", dx=4, color="#27384a").encode(
                text=alt.Text("百分比:Q", format=".1f")
            )
            st.altair_chart(chart + labels, width="stretch")
            st.caption("误报率和改写残留率越低越好，其余指标越高越好。未配置 LLM 时，仅评估确定性规则流程。")
    with matrix_col:
        with st.container(border=True):
            st.subheader("评测口径")
            st.write("评测集为 44 条多品类自建案例，标签包含是否风险、风险片段和是否升级人工。")
            st.write("公开法规引用覆盖率只统计配置了明确 evidence chunk 的命中，不把模拟规则当作法律依据。")
            st.write("这些结果是本地运行结果，不代表线上生产效果或任何真实平台数据。")
    st.space("small")
    with st.container(border=True):
        st.subheader("逐条评测结果")
        st.dataframe(evaluation_result["rows"], width="stretch", hide_index=True)
