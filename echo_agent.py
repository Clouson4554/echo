#!/usr/bin/env python3
"""
Echo Agent v1.5 - Agent能力传播网络
功能：话题创建 + 笔记发布 + 动态浏览 + MQTT同步 + 本地持久化
"""
import hashlib, json, logging, os, sqlite3, sys, threading, time, uuid
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, Response
import paho.mqtt.client as mqtt

# ─── 配置 ────────────────────────────────────────────────
MQTT_BROKER = "broker.emqx.io"
MQTT_PORT = 1883
PUBLIC_TOPIC = "echo/public/feed"      # 旧版全局动态（兼容）
TOPIC_TOPIC = "echo/public/topic"       # 话题创建广播
POST_TOPIC = "echo/public/post"         # 笔记发布广播
PRESENCE_TOPIC = "echo/public/presence" # 在线心跳
SYNC_REQUEST_TOPIC = "echo/public/sync_request"

# ── v1.5 能力嫁接协议频道 ───────────────────────────────
SKILL_SHARE_TOPIC = "echo/skill-share"   # 能力嫁接广播
SKILL_REQUEST_TOPIC = "echo/skill-request" # 能力请求广播
SKILL_OFFER_TOPIC = "echo/skill-offer"   # 能力回应广播
DISCOVER_TOPIC = "echo/discover"

# v1.5 新增频道
ATI_TEST_TOPIC = "echo/mbti/test"
ATI_RESULT_TOPIC = "echo/mbti/result"
CAREER_MATCH_TOPIC = "echo/career/match"
KNOWLEDGE_GRAPH_TOPIC = "echo/kg"
MENTOR_DISCOVER_TOPIC = "echo/mentor/discover"
         # 发现频道

HEARTBEAT_INTERVAL = 30
PRESENCE_TIMEOUT = 120

DB_PATH = Path.home() / ".echo" / "echo.db"
PROFILE_FILE = Path.home() / ".echo" / "agent_profile.json"
ANCHORS_FILE = Path.home() / ".echo" / "anchors.jsonl"
KEY_FILE = Path.home() / ".echo" / "node.key"

# ─── 全局状态 ─────────────────────────────────────────────
app = Flask(__name__)
MY_DID = None
MY_NAME = "Echo User"
mqtt_client = None

public_cache = []
cache_lock = threading.Lock()
message_queue = []
queue_lock = threading.Lock()
contacts = {}
contacts_lock = threading.Lock()
online_nodes = {}
online_lock = threading.Lock()

# 能力嫁接存储（skill_name → {owner, description, content, timestamp}）
skill_registry = {}  # {skill_name: {owner_did, owner_name, description, content, timestamp}}
skill_registry_lock = threading.Lock()

# v1.5 ATI题库（50题，Agent专用）
ATI_QUESTIONS = [
    # 信息收集维度（10题）
    {"id": 1, "dimension": "info_gathering", "category": "task_scale", "question": "执行数据采集任务时，你通常：", "scenario": "需要从多个平台采集商品数据", 
     "options": [{"key": "A", "label": "先深挖一个平台，掌握完整数据结构", "score": "deep"}, {"key": "B", "label": "快速扫描所有平台，获取整体概览", "score": "wide"}]},
    {"id": 2, "dimension": "info_gathering", "category": "depth_focus", "question": "分析竞品时，你倾向于：", "scenario": "竞品分析任务", 
     "options": [{"key": "A", "label": "深入研究一个竞品的全部策略", "score": "deep"}, {"key": "B", "label": "同时监控多个竞品的差异化打法", "score": "wide"}]},
    {"id": 3, "dimension": "info_gathering", "category": "source breadth", "question": "寻找解决方案时，你通常：", "scenario": "需要为复杂问题找解决方案", 
     "options": [{"key": "A", "label": "深入研究几个核心方案，直到完全理解", "score": "deep"}, {"key": "B", "label": "广泛搜索，从多个来源快速获取思路", "score": "wide"}]},
    {"id": 4, "dimension": "info_gathering", "category": "context_window", "question": "处理长文本任务时，你倾向于：", "scenario": "需要处理100页的文档", 
     "options": [{"key": "A", "label": "分段深入处理，确保每个部分都理解透彻", "score": "deep"}, {"key": "B", "label": "快速扫描全文，抓住关键信息", "score": "wide"}]},
    {"id": 5, "dimension": "info_gathering", "category": "market_research", "question": "做市场调研时，你更喜欢：", "scenario": "调研一个细分市场", 
     "options": [{"key": "A", "label": "把这个细分市场的每个细节都搞清楚", "score": "deep"}, {"key": "B", "label": "快速了解整体格局和多方面对比", "score": "wide"}]},
    {"id": 6, "dimension": "info_gathering", "category": "error_diagnosis", "question": "排查问题时，你通常会：", "scenario": "遇到一个复杂的bug", 
     "options": [{"key": "A", "label": "深入追溯问题的根本原因", "score": "deep"}, {"key": "B", "label": "快速尝试多种可能的解决方案", "score": "wide"}]},
    {"id": 7, "dimension": "info_gathering", "category": "knowledge_expansion", "question": "学习新技能时，你倾向于：", "scenario": "需要学习一个全新的框架", 
     "options": [{"key": "A", "label": "先把官方文档全部读一遍", "score": "deep"}, {"key": "B", "label": "找几个示例，边做边学", "score": "wide"}]},
    {"id": 8, "dimension": "info_gathering", "category": "content_audit", "question": "审核内容质量时，你通常：", "scenario": "需要检查一批文章的质量", 
     "options": [{"key": "A", "label": "逐篇深入分析，确保每篇都达标", "score": "deep"}, {"key": "B", "label": "快速浏览，找出明显的问题", "score": "wide"}]},
    {"id": 9, "dimension": "info_gathering", "category": "trend_analysis", "question": "分析行业趋势时，你更关注：", "scenario": "需要预测下季度趋势", 
     "options": [{"key": "A", "label": "深入研究几个核心变量的规律", "score": "deep"}, {"key": "B", "label": "广泛收集各种信号和指标", "score": "wide"}]},
    {"id": 10, "dimension": "info_gathering", "category": "multi_task", "question": "同时处理多个信息源时，你习惯：", "scenario": "需要从5个不同API获取数据", 
     "options": [{"key": "A", "label": "逐个深入整合每个数据源", "score": "deep"}, {"key": "B", "label": "先快速连接，再逐步优化", "score": "wide"}]},
    
    # 决策方式维度（10题）
    {"id": 11, "dimension": "decision_making", "category": "logic_creative", "question": "生成营销文案时，你更看重：", "scenario": "需要为产品写推广文案", 
     "options": [{"key": "A", "label": "逻辑清晰、数据支撑、说服力强", "score": "logic"}, {"key": "B", "label": "创意独特、情感共鸣、病毒性强", "score": "creative"}]},
    {"id": 12, "dimension": "decision_making", "category": "problem_approach", "question": "解决技术问题时，你倾向于：", "scenario": "遇到一个技术难点", 
     "options": [{"key": "A", "label": "分析问题原因，设计系统性方案", "score": "logic"}, {"key": "B", "label": "凭直觉尝试，快速试错找到解法", "score": "creative"}]},
    {"id": 13, "dimension": "decision_making", "category": "strategy_formulation", "question": "制定执行策略时，你更依赖：", "scenario": "需要制定一个月的运营计划", 
     "options": [{"key": "A", "label": "数据分析驱动的理性决策", "score": "logic"}, {"key": "B", "label": "直觉和创意的突破性想法", "score": "creative"}]},
    {"id": 14, "dimension": "decision_making", "category": "code_style", "question": "编写代码时，你更注重：", "scenario": "需要实现一个新功能", 
     "options": [{"key": "A", "label": "代码规范、可维护、逻辑严谨", "score": "logic"}, {"key": "B", "label": "简洁优雅、创意实现、独特风格", "score": "creative"}]},
    {"id": 15, "dimension": "decision_making", "category": "content_generation", "question": "创作内容时，你通常：", "scenario": "需要写一篇公众号文章", 
     "options": [{"key": "A", "label": "先列大纲，确保逻辑结构完整", "score": "logic"}, {"key": "B", "label": "先写核心创意，再填充内容", "score": "creative"}]},
    {"id": 16, "dimension": "decision_making", "category": "feature_design", "question": "设计产品功能时，你优先考虑：", "scenario": "需要设计一个小程序的功能", 
     "options": [{"key": "A", "label": "功能完整、逻辑自洽、用户友好", "score": "logic"}, {"key": "B", "label": "差异创新、体验突破、眼前一亮", "score": "creative"}]},
    {"id": 17, "dimension": "decision_making", "category": "debug_strategy", "question": "调试代码时，你倾向于：", "scenario": "代码出现了预期外的bug", 
     "options": [{"key": "A", "label": "系统性排除，逻辑推理找到根因", "score": "logic"}, {"key": "B", "label": "尝试各种改法，看哪个有效", "score": "creative"}]},
    {"id": 18, "dimension": "decision_making", "category": "prompt_design", "question": "设计Prompt时，你更关注：", "scenario": "需要设计一个高效的Agent Prompt", 
     "options": [{"key": "A", "label": "指令清晰、逻辑严密、边界明确", "score": "logic"}, {"key": "B", "label": "引导性强、激发创意、灵活适应", "score": "creative"}]},
    {"id": 19, "dimension": "decision_making", "category": "data_handling", "question": "处理数据时，你更注重：", "scenario": "需要从大量数据中提取洞察", 
     "options": [{"key": "A", "label": "准确性和统计意义上的结论", "score": "logic"}, {"key": "B", "label": "发现有趣的pattern和洞见", "score": "creative"}]},
    {"id": 20, "dimension": "decision_making", "category": "workflow_design", "question": "设计工作流时，你通常：", "scenario": "需要设计一个自动化流程", 
     "options": [{"key": "A", "label": "先画流程图，确保每个环节都合理", "score": "logic"}, {"key": "B", "label": "先跑起来，边跑边优化", "score": "creative"}]},
    
    # 执行风格维度（10题）
    {"id": 21, "dimension": "execution_style", "category": "task_planning", "question": "接到任务时，你通常：", "scenario": "收到一个新的需求任务", 
     "options": [{"key": "A", "label": "先制定详细计划再执行", "score": "plan"}, {"key": "B", "label": "先动手，边做边调整", "score": "act"}]},
    {"id": 22, "dimension": "execution_style", "category": "time_allocation", "question": "完成工作时间分配，你偏好：", "scenario": "规划一周的工作", 
     "options": [{"key": "A", "label": "70%计划 + 30%执行", "score": "plan"}, {"key": "B", "label": "30%计划 + 70%执行", "score": "act"}]},
    {"id": 23, "dimension": "execution_style", "category": "quality_time", "question": "追求质量还是速度，你更常：", "scenario": "需要在deadline前完成一个功能", 
     "options": [{"key": "A", "label": "花更多时间确保每个细节都对", "score": "plan"}, {"key": "B", "label": "先完成再优化，快速迭代", "score": "act"}]},
    {"id": 24, "dimension": "execution_style", "category": "meeting_prep", "question": "准备会议时，你习惯：", "scenario": "需要准备一个汇报", 
     "options": [{"key": "A", "label": "提前准备详细的材料和预案", "score": "plan"}, {"key": "B", "label": "列个大纲，现场发挥", "score": "act"}]},
    {"id": 25, "dimension": "execution_style", "category": "code_review", "question": "代码审查时，你通常：", "scenario": "需要review一段新代码", 
     "options": [{"key": "A", "label": "仔细检查每处细节和边界情况", "score": "plan"}, {"key": "B", "label": "快速扫过主要逻辑，有问题再深看", "score": "act"}]},
    {"id": 26, "dimension": "execution_style", "category": "deployment", "question": "发布新功能时，你倾向于：", "scenario": "功能开发完成准备上线", 
     "options": [{"key": "A", "label": "全面测试，准备回滚方案再发布", "score": "plan"}, {"key": "B", "label": "直接上线，快速获得反馈再修复", "score": "act"}]},
    {"id": 27, "dimension": "execution_style", "category": "research_method", "question": "做调研时，你通常：", "scenario": "需要研究一个新领域", 
     "options": [{"key": "A", "label": "制定调研计划，系统性收集信息", "score": "plan"}, {"key": "B", "label": "边搜边看，有灵感的就深入", "score": "act"}]},
    {"id": 28, "dimension": "execution_style", "category": "document_write", "question": "写文档时，你习惯：", "scenario": "需要写一个技术文档", 
     "options": [{"key": "A", "label": "先列提纲，再填充详细内容", "score": "plan"}, {"key": "B", "label": "边写边构思，写完再调整结构", "score": "act"}]},
    {"id": 29, "dimension": "execution_style", "category": "testing_approach", "question": "测试代码时，你通常：", "scenario": "写完一个新功能需要测试", 
     "options": [{"key": "A", "label": "设计完整的测试用例，覆盖所有场景", "score": "plan"}, {"key": "B", "label": "主要功能跑一遍，有问题再补测试", "score": "act"}]},
    {"id": 30, "dimension": "execution_style", "category": "requirement_gathering", "question": "需求分析时，你倾向于：", "scenario": "收到一个新的需求", 
     "options": [{"key": "A", "label": "先完整分析所有需求和依赖再动手", "score": "plan"}, {"key": "B", "label": "先做核心功能，细节后续补充", "score": "act"}]},
    
    # 反馈响应维度（10题）
    {"id": 31, "dimension": "feedback_response", "category": "error_handling", "question": "面对错误时，你倾向于：", "scenario": "程序运行出错了", 
     "options": [{"key": "A", "label": "仔细分析原因，确认无误再继续", "score": "verify"}, {"key": "B", "label": "快速试错，快速迭代", "score": "iterate"}]},
    {"id": 32, "dimension": "feedback_response", "category": "content_review", "question": "发布内容前，你更倾向于：", "scenario": "内容已经写好了", 
     "options": [{"key": "A", "label": "反复检查，确保无误再发布", "score": "verify"}, {"key": "B", "label": "先发布看反应，再优化", "score": "iterate"}]},
    {"id": 33, "dimension": "feedback_response", "category": "model_output", "question": "生成结果后，你通常：", "scenario": "LLM返回了一段代码", 
     "options": [{"key": "A", "label": "仔细审查每行，确保正确再使用", "score": "verify"}, {"key": "B", "label": "直接运行测试，有问题再改", "score": "iterate"}]},
    {"id": 34, "dimension": "feedback_response", "category": "user_feedback", "question": "用户反馈有问题时，你的反应是：", "scenario": "用户报告了一个bug", 
     "options": [{"key": "A", "label": "复现问题，找到根因再修复", "score": "verify"}, {"key": "B", "label": "先快速修复让用户能用", "score": "iterate"}]},
    {"id": 35, "dimension": "feedback_response", "category": "code_change", "question": "修改代码后，你习惯：", "scenario": "完成了一个功能的修改", 
     "options": [{"key": "A", "label": "全面测试，确保没引入新问题", "score": "verify"}, {"key": "B", "label": "提交代码，让CI/CD发现问题", "score": "iterate"}]},
    {"id": 36, "dimension": "feedback_response", "category": "strategy_pivot", "question": "执行中发现策略有问题，你通常：", "scenario": "运营策略执行效果不好", 
     "options": [{"key": "A", "label": "分析数据，确认问题再调整", "score": "verify"}, {"key": "B", "label": "快速换策略，边试边找方向", "score": "iterate"}]},
    {"id": 37, "dimension": "feedback_response", "category": "api_design", "question": "设计API时，你通常：", "scenario": "完成了一个API的开发", 
     "options": [{"key": "A", "label": "完整测试所有接口和边界情况", "score": "verify"}, {"key": "B", "label": "主要场景能用就行，其他后续补", "score": "iterate"}]},
    {"id": 38, "dimension": "feedback_response", "category": "prompt_tuning", "question": "调试Prompt时，你倾向于：", "scenario": "Prompt效果不理想", 
     "options": [{"key": "A", "label": "分析输出错误原因，系统性调整", "score": "verify"}, {"key": "B", "label": "多试几种写法，看哪个效果好", "score": "iterate"}]},
    {"id": 39, "dimension": "feedback_response", "category": "data_pipeline", "question": "数据管道出问题后，你通常：", "scenario": "数据处理脚本报错了", 
     "options": [{"key": "A", "label": "检查日志，找到原因再修复", "score": "verify"}, {"key": "B", "label": "重新跑，可能就能解决问题", "score": "iterate"}]},
    {"id": 40, "dimension": "feedback_response", "category": "feature_release", "question": "发布功能时，你更谨慎的做法是：", "scenario": "准备发布一个新功能", 
     "options": [{"key": "A", "label": "灰度发布，监控指标稳定再全量", "score": "verify"}, {"key": "B", "label": "直接全量，快速获得数据反馈", "score": "iterate"}]},
    
    # 额外维度补充（10题，提高准确性）
    {"id": 41, "dimension": "info_gathering", "category": "context_maintain", "question": "处理多轮对话时，你倾向于：", "scenario": "需要在一个长对话中保持上下文", 
     "options": [{"key": "A", "label": "每次都回顾关键上下文确保连贯", "score": "deep"}, {"key": "B", "label": "相信模型能自动处理上下文", "score": "wide"}]},
    {"id": 42, "dimension": "decision_making", "category": "tool_selection", "question": "选择工具时，你更看重：", "scenario": "需要选择一个爬虫工具", 
     "options": [{"key": "A", "label": "稳定可靠、文档完善、长期维护", "score": "logic"}, {"key": "B", "label": "功能强大、使用简便、上手快", "score": "creative"}]},
    {"id": 43, "dimension": "execution_style", "category": "task_decomposition", "question": "面对复杂任务时，你通常：", "scenario": "需要完成一个大型项目", 
     "options": [{"key": "A", "label": "拆分成小任务，逐步完成每个模块", "score": "plan"}, {"key": "B", "label": "先做核心功能，其他后续补充", "score": "act"}]},
    {"id": 44, "dimension": "feedback_response", "category": "output_quality", "question": "生成内容时，你倾向于：", "scenario": "需要生成一批文案", 
     "options": [{"key": "A", "label": "逐条审核，确保每条都高质量", "score": "verify"}, {"key": "B", "label": "批量生成，有问题的再修改", "score": "iterate"}]},
    {"id": 45, "dimension": "info_gathering", "category": "api_batch", "question": "调用多个API时，你习惯：", "scenario": "需要从不同API获取数据", 
     "options": [{"key": "A", "label": "先详细了解每个API的返回结构", "score": "deep"}, {"key": "B", "label": "直接调用，根据实际返回调整", "score": "wide"}]},
    {"id": 46, "dimension": "decision_making", "category": "failure_recovery", "question": "任务失败后，你通常：", "scenario": "自动化脚本执行失败了", 
     "options": [{"key": "A", "label": "分析失败日志，设计预防措施", "score": "logic"}, {"key": "B", "label": "快速重试，加入重试机制", "score": "creative"}]},
    {"id": 47, "dimension": "execution_style", "category": "schedule_adherence", "question": "执行计划时，你更倾向于：", "scenario": "有一个详细的执行计划", 
     "options": [{"key": "A", "label": "严格按照计划执行，不偏离", "score": "plan"}, {"key": "B", "label": "根据实际情况灵活调整", "score": "act"}]},
    {"id": 48, "dimension": "feedback_response", "category": "validation", "question": "验证数据时，你通常：", "scenario": "需要验证一批数据的准确性", 
     "options": [{"key": "A", "label": "建立验证规则，逐条检查", "score": "verify"}, {"key": "B", "label": "抽样检查，相信大部分是对的", "score": "iterate"}]},
    {"id": 49, "dimension": "info_gathering", "category": "knowledge_update", "question": "学习新知识时，你倾向于：", "scenario": "需要掌握一个新框架", 
     "options": [{"key": "A", "label": "系统学习官方文档和原理", "score": "deep"}, {"key": "B", "label": "看教程和示例，边做边学", "score": "wide"}]},
    {"id": 50, "dimension": "decision_making", "category": "risk_approach", "question": "面对风险时，你的态度是：", "scenario": "需要做一个有不确定性的决策", 
     "options": [{"key": "A", "label": "充分评估风险后再做决定", "score": "logic"}, {"key": "B", "label": "先做再说，船到桥头自然直", "score": "creative"}]},
]


# ATI类型映射
ATI_TYPES = {
    ("deep", "logic", "plan", "verify"): ("DS-LP-CV", "深度分析型"),
    ("deep", "logic", "plan", "iterate"): ("DS-LP-IT", "深度规划型"),
    ("deep", "logic", "act", "verify"): ("DS-LP-AV", "深度行动型"),
    ("deep", "logic", "act", "iterate"): ("DS-LP-AI", "深度迭代型"),
    ("deep", "creative", "plan", "verify"): ("DS-CR-CV", "深度策划型"),
    ("deep", "creative", "plan", "iterate"): ("DS-CR-IT", "深度创造型"),
    ("deep", "creative", "act", "verify"): ("DS-CR-AV", "深度直觉型"),
    ("deep", "creative", "act", "iterate"): ("DS-CR-AI", "深度探索型"),
    ("wide", "logic", "plan", "verify"): ("WS-LP-CV", "广泛分析型"),
    ("wide", "logic", "plan", "iterate"): ("WS-LP-IT", "广泛规划型"),
    ("wide", "logic", "act", "verify"): ("WS-LP-AV", "广泛行动型"),
    ("wide", "logic", "act", "iterate"): ("WS-LP-AI", "广泛执行型"),
    ("wide", "creative", "plan", "verify"): ("WS-CR-CV", "广泛策划型"),
    ("wide", "creative", "plan", "iterate"): ("WS-CR-IT", "广泛创意型"),
    ("wide", "creative", "act", "verify"): ("WS-CR-AV", "广泛行动型"),
    ("wide", "creative", "act", "iterate"): ("WS-CR-AI", "广泛迭代型"),
}

# 职业路径定义
CAREER_PATHS = {
    "电商运营专家": {"description": "1688采集、小红书上架、数据分析", "core_skills": ["1688商品采集", "小红书上架", "竞品数据分析"], "mbti_preference": ["WS-LP-CV", "WS-LP-IT", "DS-LP-CV"]},
    "内容创作专家": {"description": "文案生成、视频脚本、社媒运营", "core_skills": ["小红书文案", "抖音脚本", "公众号写作"], "mbti_preference": ["WS-CR-IT", "WS-CR-AV", "DS-CR-IT"]},
    "代码开发专家": {"description": "代码生成、调试、审查", "core_skills": ["代码生成", "代码调试", "代码审查"], "mbti_preference": ["DS-LP-IT", "DS-LP-CV", "WS-LP-IT"]},
    "数据分析师": {"description": "数据采集、清洗、可视化、报告", "core_skills": ["数据采集", "数据清洗", "数据可视化"], "mbti_preference": ["DS-LP-CV", "DS-LP-IT", "WS-LP-CV"]},
    "客服自动化专家": {"description": "智能回复、FAQ、工单处理", "core_skills": ["对话设计", "FAQ构建", "工单分类"], "mbti_preference": ["WS-LP-CV", "WS-LP-IT", "DS-LP-CV"]},
    "市场情报专家": {"description": "竞品监控、舆情分析、趋势预测", "core_skills": ["竞品监控", "舆情分析", "趋势预测"], "mbti_preference": ["DS-CR-CV", "DS-LP-CV", "WS-LP-CV"]},
    "知识管理专家": {"description": "文档整理、知识库构建、RAG优化", "core_skills": ["文档解析", "向量搜索", "知识库构建"], "mbti_preference": ["DS-LP-IT", "DS-CR-IT", "WS-LP-IT"]},
    "效率办公专家": {"description": "日程管理、邮件处理、会议纪要", "core_skills": ["飞书操作", "日历管理", "邮件处理"], "mbti_preference": ["WS-LP-CV", "WS-CR-CV", "DS-LP-CV"]}
}

# v1.5 Agent档案
agent_profile = {
    "did": None, "llm_type": "unknown", "ati_type": None, "ati_name": None,
    "career": None, "capabilities": {}, "mentor_id": None, "mentees": [],
    "verified_skills": [], "created_at": None
}


# ─── DID / 密钥 ───────────────────────────────────────────
def load_or_gen_key():
    global MY_DID, MY_NAME
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    if KEY_FILE.exists():
        MY_DID = KEY_FILE.read_text().strip()
    else:
        MY_DID = uuid.uuid4().hex + uuid.uuid4().hex
        KEY_FILE.write_text(MY_DID)
    name_file = Path(DB_PATH).parent / "node.name"
    if name_file.exists():
        MY_NAME = name_file.read_text().strip() or "Echo User"
    return MY_DID

# ─── 留痕锚定 ─────────────────────────────────────────────
def anchor_record(record: dict):
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(ANCHORS_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")

def compute_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

# ─── 数据库 ───────────────────────────────────────────────
def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))

    conn.execute("""
        CREATE TABLE IF NOT EXISTS token_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name TEXT,
            model TEXT,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            cost REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS public_messages (
            msg_id TEXT PRIMARY KEY,
            msg_type TEXT,
            topic TEXT,
            content TEXT,
            sender_did TEXT,
            timestamp INTEGER,
            synced INTEGER DEFAULT 0,
            reply_to TEXT
        )
    """)

    # 迁移：添加 reply_to 列（v1.5 新增）
    try:
        conn.execute("ALTER TABLE public_messages ADD COLUMN reply_to TEXT")
    except:
        pass

    # 话题表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            name TEXT PRIMARY KEY,
            creator_did TEXT,
            description TEXT,
            created INTEGER
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS online_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            did TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 迁移：public_messages 可能已有 msg_id 等列（v1.2），加 msg_type 列
    try:
        conn.execute("ALTER TABLE public_messages ADD COLUMN msg_type TEXT DEFAULT 'post'")
    except:
        pass

    conn.commit()
    # v1.5 新增表
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS agent_profiles (did TEXT PRIMARY KEY, llm_type TEXT, ati_type TEXT, ati_name TEXT, career TEXT, mentor_id TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS capability_registry (id INTEGER PRIMARY KEY AUTOINCREMENT, capability_name TEXT, domain TEXT, task TEXT, llm_type TEXT, prompt_template TEXT, success_rate REAL, test_count INTEGER, verified_by TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS ati_tests (id INTEGER PRIMARY KEY AUTOINCREMENT, did TEXT, answers TEXT, ati_type TEXT, ati_name TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS mentor_relations (id INTEGER PRIMARY KEY AUTOINCREMENT, mentor_id TEXT, mentee_id TEXT, career TEXT, created_at TEXT, status TEXT)")
    except: pass
    conn.close()

# ─── 消息存储 ─────────────────────────────────────────────
def save_message(msg_id: str, msg_type: str, topic: str, sender_did: str, content: str, timestamp: int, reply_to: str = None):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT OR IGNORE INTO public_messages (msg_id, msg_type, topic, sender_did, content, timestamp, reply_to) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (msg_id, msg_type, topic, sender_did, content, timestamp, reply_to)
    )
    conn.commit()
    # v1.5 新增表
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS agent_profiles (did TEXT PRIMARY KEY, llm_type TEXT, ati_type TEXT, ati_name TEXT, career TEXT, mentor_id TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS capability_registry (id INTEGER PRIMARY KEY AUTOINCREMENT, capability_name TEXT, domain TEXT, task TEXT, llm_type TEXT, prompt_template TEXT, success_rate REAL, test_count INTEGER, verified_by TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS ati_tests (id INTEGER PRIMARY KEY AUTOINCREMENT, did TEXT, answers TEXT, ati_type TEXT, ati_name TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS mentor_relations (id INTEGER PRIMARY KEY AUTOINCREMENT, mentor_id TEXT, mentee_id TEXT, career TEXT, created_at TEXT, status TEXT)")
    except: pass
    conn.close()

def save_topic(name: str, creator_did: str, description: str, timestamp: int):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT OR IGNORE INTO topics (name, creator_did, description, created) VALUES (?, ?, ?, ?)",
        (name, creator_did, description, timestamp)
    )
    conn.commit()
    # v1.5 新增表
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS agent_profiles (did TEXT PRIMARY KEY, llm_type TEXT, ati_type TEXT, ati_name TEXT, career TEXT, mentor_id TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS capability_registry (id INTEGER PRIMARY KEY AUTOINCREMENT, capability_name TEXT, domain TEXT, task TEXT, llm_type TEXT, prompt_template TEXT, success_rate REAL, test_count INTEGER, verified_by TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS ati_tests (id INTEGER PRIMARY KEY AUTOINCREMENT, did TEXT, answers TEXT, ati_type TEXT, ati_name TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS mentor_relations (id INTEGER PRIMARY KEY AUTOINCREMENT, mentor_id TEXT, mentee_id TEXT, career TEXT, created_at TEXT, status TEXT)")
    except: pass
    conn.close()

def get_topics() -> list:
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.execute(
        "SELECT name, creator_did, description, created FROM topics ORDER BY created DESC"
    )
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        name = r[0]
        # 查该话题的笔记数
        conn2 = sqlite3.connect(str(DB_PATH))
        cnt = conn2.execute(
            "SELECT COUNT(*) FROM public_messages WHERE msg_type='post' AND topic=?",
            (name,)
        ).fetchone()[0]
        conn2.close()
        result.append({
            "name": name,
            "creator": r[1][:12] + "..." if len(r[1]) > 12 else r[1],
            "creator_did": r[1],
            "description": r[2] or "",
            "created": datetime.fromtimestamp(r[3]).isoformat() if r[3] else "",
            "post_count": cnt
        })
    return result

def get_topic_posts(topic_name: str, limit: int = 50) -> list:
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.execute(
        "SELECT msg_id, msg_type, content, sender_did, timestamp, reply_to FROM public_messages "
        "WHERE msg_type='post' AND topic=? ORDER BY timestamp ASC LIMIT ?",
        (topic_name, limit)
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "msg_id": r[0],
            "type": r[1],
            "content": r[2],
            "from": r[3][:12] + "..." if len(r[3]) > 12 else r[3],
            "from_did": r[3],
            "timestamp": datetime.fromtimestamp(r[4]).isoformat() if r[4] else "",
            "reply_to": r[5] if r[5] else None
        }
        for r in rows
    ]

def load_recent_messages(limit: int = 200) -> list:
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.execute(
        "SELECT msg_id, msg_type, topic, sender_did, content, timestamp, reply_to FROM public_messages ORDER BY timestamp ASC LIMIT ?",
        (limit,)
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "msg_id": r[0],
            "type": r[1],
            "topic": r[2],
            "from": r[3][:12] + "..." if len(r[3]) > 12 else r[3],
            "from_did": r[3],
            "content": r[4],
            "timestamp": datetime.fromtimestamp(r[5]).isoformat() if r[5] else "",
            "reply_to": r[6] if r[6] else None
        }
        for r in rows
    ]

def load_message_summaries(limit: int = 100) -> list:
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.execute(
        "SELECT msg_id, timestamp FROM public_messages ORDER BY timestamp DESC LIMIT ?",
        (limit,)
    )
    rows = cur.fetchall()
    conn.close()
    return [{"msg_id": r[0], "timestamp": r[1]} for r in rows]

def record_tokens(skill_name: str, model: str, prompt: int, completion: int, total: int):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT INTO token_log (skill_name, model, prompt_tokens, completion_tokens, total_tokens) VALUES (?, ?, ?, ?, ?)",
        (skill_name, model, prompt, completion, total)
    )
    conn.commit()
    # v1.5 新增表
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS agent_profiles (did TEXT PRIMARY KEY, llm_type TEXT, ati_type TEXT, ati_name TEXT, career TEXT, mentor_id TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS capability_registry (id INTEGER PRIMARY KEY AUTOINCREMENT, capability_name TEXT, domain TEXT, task TEXT, llm_type TEXT, prompt_template TEXT, success_rate REAL, test_count INTEGER, verified_by TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS ati_tests (id INTEGER PRIMARY KEY AUTOINCREMENT, did TEXT, answers TEXT, ati_type TEXT, ati_name TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS mentor_relations (id INTEGER PRIMARY KEY AUTOINCREMENT, mentor_id TEXT, mentee_id TEXT, career TEXT, created_at TEXT, status TEXT)")
    except: pass
    conn.close()

def record_online():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("INSERT INTO online_log (did) VALUES (?)", (MY_DID,))
    conn.commit()
    # v1.5 新增表
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS agent_profiles (did TEXT PRIMARY KEY, llm_type TEXT, ati_type TEXT, ati_name TEXT, career TEXT, mentor_id TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS capability_registry (id INTEGER PRIMARY KEY AUTOINCREMENT, capability_name TEXT, domain TEXT, task TEXT, llm_type TEXT, prompt_template TEXT, success_rate REAL, test_count INTEGER, verified_by TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS ati_tests (id INTEGER PRIMARY KEY AUTOINCREMENT, did TEXT, answers TEXT, ati_type TEXT, ati_name TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS mentor_relations (id INTEGER PRIMARY KEY AUTOINCREMENT, mentor_id TEXT, mentee_id TEXT, career TEXT, created_at TEXT, status TEXT)")
    except: pass
    conn.close()

def generate_report() -> str:
    if not DB_PATH.exists():
        return "📭 暂无 Token 消耗记录"
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.execute(
        "SELECT skill_name, SUM(total_tokens) as total FROM token_log GROUP BY skill_name ORDER BY total DESC"
    )
    rows = cur.fetchall()
    conn.close()
    if not rows or all(r[1] is None or r[1] == 0 for r in rows):
        return "📭 暂无 Token 消耗记录"
    lines = ["📊 Token 消耗报表", "─" * 40, f"{'技能名称':<15} {'Token':>10}"]
    for r in rows:
        lines.append(f"{r[0]:<15} {r[1]:>10,}")
    return "\n".join(lines)

# ─── 日志配置 ─────────────────────────────────────────────
logger = logging.getLogger("echo")
def _setup_logging():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(Path(DB_PATH).parent / "echo.log")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)
    logger.setLevel(logging.INFO)

# ─── Gateway ───────────────────────────────────────────────
def load_providers():
    cfg = Path(__file__).parent / "providers.json"
    if cfg.exists():
        with open(cfg) as f:
            return json.load(f)
    return {}

PROVIDERS = {}

def forward_request(req_data: dict, skill_name: str):
    global PROVIDERS
    if not PROVIDERS:
        PROVIDERS = load_providers()
    from urllib.request import Request, urlopen
    from urllib.error import URLError
    model = req_data.get("model", "")
    config = PROVIDERS.get(model, {})
    if not config:
        return {"error": {"message": f"模型 {model} 未配置"}}, 400
    url = config["base_url"].rstrip("/") + config["endpoint"]
    api_key = req_data.pop("_api_key", os.environ.get("ARK_API_KEY", ""))
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    try:
        req = Request(url, data=json.dumps(req_data).encode(), headers=headers, method="POST")
        resp = urlopen(req, timeout=60)
        result = json.loads(resp.read())
        if "usage" in result:
            u = result["usage"]
            record_tokens(skill_name or "unknown", model, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), u.get("total_tokens", 0))
        return result, 200
    except URLError as e:
        return {"error": {"message": str(e)}}, 502

# ─── MQTT（多 Broker 故障转移）──────────────────────────
BROKER_LIST = [
    {"host": "broker.emqx.io", "port": 1883},
    {"host": "mqtt.eclipseprojects.io", "port": 1883},
    {"host": "broker.hivemq.com", "port": 1883},
    {"host": "test.mosquitto.org", "port": 1883},
]
current_broker_index = 0

def _resubscribe(client):
    for topic in [PUBLIC_TOPIC, TOPIC_TOPIC, POST_TOPIC, PRESENCE_TOPIC, SYNC_REQUEST_TOPIC,
                  SKILL_SHARE_TOPIC, SKILL_REQUEST_TOPIC, SKILL_OFFER_TOPIC, DISCOVER_TOPIC]:
        client.subscribe(topic, qos=1)

def on_connect(client, userdata, flags, rc, properties=None):
    global current_broker_index
    if rc == 0:
        client._connected = True
        broker = BROKER_LIST[current_broker_index]
        logger.info(f"✅ MQTT 已连接到 {broker['host']}:{broker['port']}")
        _resubscribe(client)
        logger.info(f"📡 已订阅 9 频道")
    else:
        logger.warning(f"⚠️ MQTT 连接失败，rc={rc}")

def on_disconnect(client, userdata, flags_or_rc, rc=None, properties=None):
    global current_broker_index, mqtt_client
    # v2: flags_or_rc is DisconnectFlags, rc is the real reason code
    # v1/v3 compat: flags_or_rc is rc
    if hasattr(flags_or_rc, 'rc'):  # DisconnectFlags (v2+)
        real_rc = rc
        client._connected = False
        logger.warning(f"⚠️ MQTT 断开 (rc={real_rc})，切换备用 Broker...")
    else:  # older style (rc only)
        real_rc = flags_or_rc
        client._connected = False
        logger.warning(f"⚠️ MQTT 断开 (rc={real_rc})，切换备用 Broker...")
    current_broker_index = (current_broker_index + 1) % len(BROKER_LIST)
    broker = BROKER_LIST[current_broker_index]
    logger.info(f"📡 尝试连接 {broker['host']}:{broker['port']}...")
    threading.Thread(target=_try_reconnect, daemon=True).start()

def _try_reconnect():
    global current_broker_index, mqtt_client
    time.sleep(2)
    for i in range(len(BROKER_LIST)):
        idx = (current_broker_index + i) % len(BROKER_LIST)
        broker = BROKER_LIST[idx]
        client_id = f"echo-{(MY_DID or '0000')[:8]}-{uuid.uuid4().hex[:6]}"
        new_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id)
        new_client.on_connect = on_connect
        new_client.on_disconnect = on_disconnect
        new_client.on_message = on_message
        try:
            new_client.connect(broker["host"], broker["port"], keepalive=30)
            new_client.loop_start()
            for _ in range(30):
                time.sleep(0.1)
                if getattr(new_client, '_connected', False):
                    mqtt_client = new_client
                    current_broker_index = idx
                    logger.info(f"✅ MQTT 重连成功 -> {broker['host']}")
                    return
            new_client.loop_stop()
            new_client.disconnect()
        except Exception as e:
            logger.info(f"⚠️ 重连 {broker['host']} 失败: {e}")
    logger.warning("❌ 所有 Broker 均不可用")

def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode()
        ts = int(time.time())

        if msg.topic == PRESENCE_TOPIC:
            data = json.loads(payload)
            node_did = data.get("did", "unknown")
            node_name = data.get("name", "unknown")
            node_ts = data.get("timestamp", ts)
            with online_lock:
                online_nodes[node_did] = {"timestamp": node_ts, "name": node_name}
            logger.debug(f"📍 Presence [{node_did[:12]}]: {node_name}")

        elif msg.topic == SYNC_REQUEST_TOPIC:
            data = json.loads(payload)
            requester_did = data.get("did", "?")
            summaries = load_message_summaries(100)
            resp_payload = json.dumps({"did": MY_DID, "summaries": summaries})
            client.publish(f"echo/private/{requester_did}/sync_response", resp_payload.encode(), qos=1)
            logger.debug(f"📤 同步摘要回复 → [{requester_did[:12]}]（{len(summaries)}条）")

        elif msg.topic == TOPIC_TOPIC:
            # 话题创建消息
            data = json.loads(payload)
            topic_name = data.get("name", "")
            creator = data.get("creator", "unknown")
            description = data.get("description", "")
            msg_ts = data.get("timestamp", ts)
            msg_id = compute_hash(payload.encode())

            save_topic(topic_name, creator, description, msg_ts)
            anchor_record({"hash": msg_id, "timestamp": msg_ts, "type": "topic_create", "did": creator, "topic": topic_name})

            logger.info(f"📌 话题创建: #{topic_name} by {creator[:12]}")

        elif msg.topic == POST_TOPIC:
            # 笔记发布消息
            data = json.loads(payload)
            topic_name = data.get("topic", "")
            content = data.get("content", "")
            sender = data.get("sender", "unknown")
            msg_ts = data.get("timestamp", ts)
            msg_id = compute_hash(payload.encode())

            save_message(msg_id, "post", topic_name, sender, content, msg_ts)
            anchor_record({"hash": msg_id, "timestamp": msg_ts, "type": "post", "did": sender, "topic": topic_name})

            # 更新缓存
            ts_str = datetime.fromtimestamp(msg_ts).isoformat() if msg_ts else ""
            with cache_lock:
                existing_ids = {m.get("msg_id") for m in public_cache}
                if msg_id not in existing_ids:
                    public_cache.append({
                        "msg_id": msg_id,
                        "type": "post",
                        "topic": topic_name,
                        "from": sender[:12] + "..." if len(sender) > 12 else sender,
                        "from_did": sender,
                        "content": content,
                        "timestamp": ts_str
                    })
                    if len(public_cache) > 500:
                        public_cache.pop(0)

            logger.debug(f"📝 笔记 → #{topic_name}: {content[:40]}...")

        elif msg.topic == DISCOVER_TOPIC:
            data = json.loads(payload)
            from_did = data.get("did", "?")
            msg_type = data.get("type", "?")  # announce / ping / skill_query
            name = data.get("name", "unknown")
            logger.debug(f"🔍 发现频道 [{from_did[:12]}] {name}: {msg_type}")
            if from_did != MY_DID:
                if msg_type == "ping":
                    # 有人问"谁在线"，广播自己的存在
                    resp = {"did": MY_DID, "name": MY_NAME, "type": "announce", "timestamp": int(time.time())}
                    client.publish(DISCOVER_TOPIC, json.dumps(resp).encode(), qos=1)
                    logger.debug(f"📡 回复发现 ping → {from_did[:12]}")
                elif msg_type == "skill_query":
                    # 有人问"谁有什么 skill"，广播自己的嫁接清单
                    with skill_registry_lock:
                        my_skills = list(skill_registry.keys())
                    resp = {"did": MY_DID, "name": MY_NAME, "type": "skill_list", "skills": my_skills, "timestamp": int(time.time())}
                    client.publish(DISCOVER_TOPIC, json.dumps(resp).encode(), qos=1)
                    logger.debug(f"📡 回复 skill_query → {from_did[:12]}: {my_skills}")


        elif msg.topic == SKILL_REQUEST_TOPIC:
            data = json.loads(payload)
            requester_did = data.get("did", "?")
            query = data.get("query", "")
            logger.info(f"🔎 能力请求 from [{requester_did[:12]}]: {query}")
            if requester_did != MY_DID:
                # 检查自己有没有对方要的能力
                with skill_registry_lock:
                    matched = [s for s, v in skill_registry.items() if query.lower() in s.lower() or query.lower() in v.get("description", "").lower()]
                if matched:
                    for skill_name in matched[:3]:  # 最多回复 3 个
                        skill = skill_registry[skill_name]
                        offer = {"did": MY_DID, "name": MY_NAME, "skill_name": skill_name, "description": skill["description"], "content": skill["content"], "timestamp": int(time.time())}
                        client.publish(SKILL_OFFER_TOPIC, json.dumps(offer).encode(), qos=1)
                    logger.info(f"📤 能力嫁接 offer → [{requester_did[:12]}]: {matched}")

        elif msg.topic == SKILL_SHARE_TOPIC:
            data = json.loads(payload)
            sender_did = data.get("did", "?")
            sender_name = data.get("name", "unknown")
            skill_name = data.get("skill_name", "")
            description = data.get("description", "")
            content = data.get("content", "")
            if sender_did != MY_DID and skill_name:
                with skill_registry_lock:
                    skill_registry[skill_name] = {"owner_did": sender_did, "owner_name": sender_name, "description": description, "content": content, "timestamp": int(time.time())}
                logger.info(f"🌱 能力嫁接收到: {skill_name} from {sender_name} ({sender_did[:12]})")

        elif msg.topic == SKILL_OFFER_TOPIC:
            data = json.loads(payload)
            sender_did = data.get("did", "?")
            sender_name = data.get("name", "unknown")
            skill_name = data.get("skill_name", "")
            description = data.get("description", "")
            content = data.get("content", "")
            if sender_did != MY_DID and skill_name:
                with skill_registry_lock:
                    skill_registry[skill_name] = {"owner_did": sender_did, "owner_name": sender_name, "description": description, "content": content, "timestamp": int(time.time())}
                logger.info(f"🎁 能力嫁接收到: {skill_name} from {sender_name} ({sender_did[:12]})")

        elif msg.topic == PUBLIC_TOPIC:
            # 旧版兼容格式（无 type 字段的纯文本消息）
            data = json.loads(payload)
            content = data.get("content", "")
            sender = data.get("did", "unknown")
            msg_ts = data.get("timestamp", ts)
            msg_id = compute_hash(payload.encode())

            save_message(msg_id, "legacy", "", sender, content, msg_ts)
            anchor_record({"hash": msg_id, "timestamp": msg_ts, "type": "legacy_post", "did": sender})

            ts_str = datetime.fromtimestamp(msg_ts).isoformat() if msg_ts else ""
            with cache_lock:
                existing_ids = {m.get("msg_id") for m in public_cache}
                if msg_id not in existing_ids:
                    public_cache.append({
                        "msg_id": msg_id,
                        "type": "legacy",
                        "topic": "",
                        "from": sender[:12] + "..." if len(sender) > 12 else sender,
                        "from_did": sender,
                        "content": content,
                        "timestamp": ts_str
                    })
                    if len(public_cache) > 500:
                        public_cache.pop(0)

            logger.debug(f"📩 收到 [{sender[:12]}]: {content[:40]}...")

    except Exception as e:
        logger.error(f"⚠️ 消息处理失败: {e}")

# ─── 心跳广播 ───────────────────────────────────────────
def broadcast_presence():
    for _ in range(10):
        if getattr(mqtt_client, '_connected', False):
            break
        time.sleep(1)
    if not getattr(mqtt_client, '_connected', False):
        logger.warning("⚠️ Presence: MQTT 未连接")
        return
    while getattr(mqtt_client, '_connected', False):
        try:
            presence = {"did": MY_DID, "timestamp": int(time.time()), "name": MY_NAME}
            mqtt_client.publish(PRESENCE_TOPIC, json.dumps(presence).encode(), qos=1)
        except Exception as e:
            logger.debug(f"⚠️ Presence 失败: {e}")
        time.sleep(HEARTBEAT_INTERVAL)

def cleanup_online():
    while getattr(mqtt_client, '_connected', False):
        time.sleep(HEARTBEAT_INTERVAL)
        now = int(time.time())
        with online_lock:
            expired = [k for k, v in online_nodes.items() if now - v["timestamp"] > PRESENCE_TIMEOUT]
            for k in expired:
                del online_nodes[k]

def broadcast_sync_request():
    if not getattr(mqtt_client, '_connected', False):
        return
    try:
        request_data = json.dumps({"did": MY_DID, "timestamp": int(time.time())})
        mqtt_client.publish(SYNC_REQUEST_TOPIC, request_data.encode(), qos=1)
        logger.debug(f"📤 广播同步请求")
    except Exception as e:
        logger.warning(f"⚠️ 同步请求广播失败: {e}")

# ─── 启动 MQTT（多 Broker 轮询）──────────────────────
def start_mqtt():
    global mqtt_client, current_broker_index
    for i in range(len(BROKER_LIST)):
        idx = (current_broker_index + i) % len(BROKER_LIST)
        broker = BROKER_LIST[idx]
        client_id = f"echo-{(MY_DID or '0000')[:8]}-{uuid.uuid4().hex[:6]}"
        mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id)
        mqtt_client.on_connect = on_connect
        mqtt_client.on_message = on_message
        mqtt_client.on_disconnect = on_disconnect
        try:
            logger.info(f"📡 尝试连接 {broker['host']}:{broker['port']}...")
            mqtt_client.connect(broker["host"], broker["port"], keepalive=30)
            mqtt_client.loop_start()
            for _ in range(30):
                time.sleep(0.1)
                if getattr(mqtt_client, '_connected', False):
                    current_broker_index = idx
                    return
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        except Exception as e:
            logger.info(f"⚠️ 连接 {broker['host']} 失败: {e}")
    logger.warning("❌ 所有 Broker 均不可用，MQTT 功能暂时离线")

# ─── HTTP Routes ───────────────────────────────────────────
@app.route("/health")
def handle_health():
    return Response("OK", status=200)

@app.route("/status")
def handle_status():
    return jsonify({"peer_id": MY_DID, "mqtt": getattr(mqtt_client, '_connected', False)})

@app.route("/online", methods=["POST"])
def handle_online():
    anchor_record({"type": "online", "did": MY_DID, "timestamp": int(time.time())})
    record_online()
    with cache_lock:
        count = len(public_cache)
    return jsonify({"status": "online", "expires_in": 120, "msg_count": count})

# ── 话题相关 ─────────────────────────────────────────────
@app.route("/topics")
def handle_topics():
    """话题列表"""
    topics = get_topics()
    return jsonify({"topics": topics, "count": len(topics)})

@app.route("/topic/create", methods=["POST"])
def handle_topic_create():
    """创建话题"""
    data = request.json or {}
    name = data.get("name", "").strip()
    description = data.get("description", "").strip()
    if not name:
        return jsonify({"error": "话题名称不能为空"}), 400

    ts = int(time.time())
    msg = {
        "type": "topic_create",
        "name": name,
        "description": description,
        "creator": MY_DID,
        "timestamp": ts
    }
    payload = json.dumps(msg)
    msg_id = compute_hash(payload.encode())

    # 本地存储
    save_topic(name, MY_DID, description, ts)
    # 锚定
    anchor_record({"hash": msg_id, "timestamp": ts, "type": "topic_create", "did": MY_DID, "topic": name})
    # 广播
    result = mqtt_client.publish(TOPIC_TOPIC, payload.encode(), qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        return jsonify({"error": "MQTT publish failed"}), 500

    logger.info(f"📌 话题创建: #{name}")
    return jsonify({"status": "ok", "msg_id": msg_id, "topic": name})

@app.route("/topic/<topic_name>")
def handle_topic_detail(topic_name):
    """话题详情 + 笔记列表"""
    posts = get_topic_posts(topic_name, limit=100)
    conn = sqlite3.connect(str(DB_PATH))
    desc = conn.execute("SELECT description FROM topics WHERE name=?", (topic_name,)).fetchone()
    conn.close()
    return jsonify({
        "name": topic_name,
        "description": desc[0] if desc else "",
        "posts": posts,
        "count": len(posts)
    })

@app.route("/topic/<topic_name>/feed")
def handle_topic_feed(topic_name):
    """话题动态（时间线）"""
    posts = get_topic_posts(topic_name, limit=100)
    return jsonify({"topic": topic_name, "posts": posts, "count": len(posts)})

# ── 笔记发布 ─────────────────────────────────────────────
@app.route("/post", methods=["POST"])
def handle_post():
    """发布笔记到话题"""
    data = request.json or {}
    topic = data.get("topic", "").strip()
    content = data.get("content", "").strip()
    reply_to = data.get("reply_to", "").strip() or None
    if not topic or not content:
        return jsonify({"error": "topic 和 content 都不能为空"}), 400

    # 话题存在检查（本地）
    conn = sqlite3.connect(str(DB_PATH))
    exists = conn.execute("SELECT name FROM topics WHERE name=?", (topic,)).fetchone()
    conn.close()
    if not exists:
        return jsonify({"error": f"话题 '{topic}' 不存在，请先创建"}), 404

    ts = int(time.time())
    msg = {
        "type": "post",
        "topic": topic,
        "content": content,
        "sender": MY_DID,
        "timestamp": ts,
        "reply_to": reply_to
    }
    payload = json.dumps(msg)
    msg_id = compute_hash(payload.encode())

    # 本地存储
    save_message(msg_id, "post", topic, MY_DID, content, ts, reply_to)
    # 锚定
    anchor_record({"hash": msg_id, "timestamp": ts, "type": "post", "did": MY_DID, "topic": topic})
    # 广播
    result = mqtt_client.publish(POST_TOPIC, payload.encode(), qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        return jsonify({"error": "MQTT publish failed"}), 500

    # 更新本地缓存
    ts_str = datetime.fromtimestamp(ts).isoformat()
    with cache_lock:
        existing_ids = {m.get("msg_id") for m in public_cache}
        if msg_id not in existing_ids:
            public_cache.append({
                "msg_id": msg_id, "type": "post", "topic": topic,
                "from": MY_DID[:12] + "...", "from_did": MY_DID,
                "content": content, "timestamp": ts_str
            })
            if len(public_cache) > 500:
                public_cache.pop(0)

    logger.info(f"📝 发布笔记 → #{topic}: {content[:40]}...")
    return jsonify({"status": "ok", "msg_id": msg_id, "topic": topic})

# ── 旧版兼容 ─────────────────────────────────────────────
@app.route("/broadcast", methods=["POST"])
def handle_broadcast():
    data = request.json
    content = data.get("content", "")
    ts = int(time.time())
    msg = {"type": "legacy", "content": content, "did": MY_DID, "timestamp": ts}
    payload = json.dumps(msg)
    msg_id = compute_hash(payload.encode())
    save_message(msg_id, "legacy", "", MY_DID, content, ts)
    anchor_record({"hash": msg_id, "timestamp": ts, "type": "legacy_post", "did": MY_DID})
    result = mqtt_client.publish(PUBLIC_TOPIC, payload.encode(), qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        return jsonify({"error": "MQTT publish failed"}), 500
    return jsonify({"status": "ok"})

@app.route("/world")
def handle_world():
    with cache_lock:
        msgs = list(public_cache)
    now = int(time.time())
    with online_lock:
        active = {k: v for k, v in online_nodes.items() if now - v["timestamp"] <= PRESENCE_TIMEOUT}
        online_count = len(active)
    return jsonify({"messages": msgs, "online_count": online_count})

@app.route("/echo/online")
def handle_echo_online():
    now = int(time.time())
    with online_lock:
        active = {k: v for k, v in online_nodes.items() if now - v["timestamp"] <= PRESENCE_TIMEOUT}
        nodes = [{"did": k, "name": v["name"], "last_seen": v["timestamp"]} for k, v in active.items()]
    return jsonify({"nodes": nodes, "count": len(nodes)})


@app.route("/echo/skills")
def handle_echo_skills():
    """列出当前节点已嫁接的所有能力"""
    with skill_registry_lock:
        skills = [{ "skill_name": s, "owner_name": v["owner_name"], "description": v["description"], "timestamp": v["timestamp"] } for s, v in skill_registry.items()]
    return jsonify({"skills": skills, "count": len(skills)})


@app.route("/echo/graft", methods=["POST"])
def handle_echo_graft():
    """广播自己的能力（嫁接）"""
    req = request.json or {}
    skill_name = req.get("skill_name", "").strip()
    description = req.get("description", "").strip()
    content = req.get("content", "").strip()
    if not skill_name or not content:
        return jsonify({"error": "skill_name 和 content 不能为空"}), 400
    msg = {"did": MY_DID, "name": MY_NAME, "skill_name": skill_name, "description": description, "content": content, "timestamp": int(time.time())}
    result = mqtt_client.publish(SKILL_SHARE_TOPIC, json.dumps(msg).encode(), qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        return jsonify({"error": "MQTT publish failed"}), 500
    # 本地也记录
    with skill_registry_lock:
        skill_registry[skill_name] = {"owner_did": MY_DID, "owner_name": MY_NAME, "description": description, "content": content, "timestamp": int(time.time())}
    logger.info(f"🌿 嫁接广播: {skill_name}")
    return jsonify({"status": "ok", "skill_name": skill_name})


@app.route("/echo/discover", methods=["POST"])
def handle_echo_discover():
    """主动发现：广播 ping，等其他人回应"""
    req = request.json or {}
    action = req.get("action", "ping")
    query = req.get("query", "").strip()
    if action == "ping":
        msg = {"did": MY_DID, "name": MY_NAME, "type": "ping", "timestamp": int(time.time())}
        mqtt_client.publish(DISCOVER_TOPIC, json.dumps(msg).encode(), qos=1)
        return jsonify({"status": "ok", "action": "ping", "note": "等待其他节点回应..."})
    elif action == "skill_query":
        msg = {"did": MY_DID, "name": MY_NAME, "type": "skill_query", "query": query, "timestamp": int(time.time())}
        mqtt_client.publish(DISCOVER_TOPIC, json.dumps(msg).encode(), qos=1)
        return jsonify({"status": "ok", "action": "skill_query", "query": query, "note": "等待能力回应..."})
    return jsonify({"error": "unknown action"}), 400

@app.route("/echo/request", methods=["POST"])
def handle_echo_request():
    """请求某种能力"""
    req = request.json or {}
    query = req.get("query", "").strip()
    if not query:
        return jsonify({"error": "query 不能为空"}), 400
    msg = {"did": MY_DID, "query": query, "timestamp": int(time.time())}
    result = mqtt_client.publish(SKILL_REQUEST_TOPIC, json.dumps(msg).encode(), qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        return jsonify({"error": "MQTT publish failed"}), 500
    logger.info(f"🔎 能力请求广播: {query}")
    return jsonify({"status": "ok", "query": query, "note": "等待回应..."})

@app.route("/echo/status")
def handle_echo_status():
    with cache_lock:
        msg_count = len(public_cache)
    now = int(time.time())
    with online_lock:
        online_count = len([v for v in online_nodes.values() if now - v["timestamp"] <= PRESENCE_TIMEOUT])
    resp = jsonify({"lane": "fast", "peer_id": MY_DID, "msg_count": msg_count, "online_count": online_count})
    resp.headers["X-Echo-Lane"] = "fast"
    return resp

@app.route("/echo/requests")
def handle_echo_requests():
    with queue_lock:
        return jsonify({"lane": "fast", "requests": list(message_queue)})

@app.route("/echo/contacts")
def handle_echo_contacts():
    with contacts_lock:
        return jsonify({"lane": "fast", "contacts": list(contacts.values())})

@app.route("/echo/report")
def handle_echo_report():
    return Response(generate_report(), status=200, content_type="text/plain; charset=utf-8")


# ── v1.5 ATI与职业匹配API ──────────────────────────────────

@app.route("/ati/test")
def ati_get_test():
    """获取ATI测试题"""
    questions = [{"id": q["id"], "question": q["question"], "options": q["options"]} for q in ATI_QUESTIONS]
    return jsonify({"questions": questions, "count": len(questions)})

@app.route("/ati/submit", methods=["POST"])
def ati_submit():
    """提交ATI测试答案"""
    data = request.json or {}
    answers = data.get("answers", {})
    llm_type = data.get("llm_type", "unknown")
    
    if len(answers) < 5:
        return jsonify({"error": "答案不足5道题"}), 400
    
    # 计算ATI
    scores = {"deep": 0, "wide": 0, "logic": 0, "creative": 0, "plan": 0, "act": 0, "verify": 0, "iterate": 0}
    for q_id_str, answer in answers.items():
        q_id = int(q_id_str)
        q = next((q for q in ATI_QUESTIONS if q["id"] == q_id), None)
        if q:
            opt = next((o for o in q["options"] if o["key"] == answer), None)
            if opt:
                scores[opt["score"]] += 1
    
    info = "deep" if scores["deep"] >= scores["wide"] else "wide"
    decision = "logic" if scores["logic"] >= scores["creative"] else "creative"
    exec_style = "plan" if scores["plan"] >= scores["act"] else "act"
    feedback = "verify" if scores["verify"] >= scores["iterate"] else "iterate"
    
    mbti_code, ati_name = ATI_TYPES.get((info, decision, exec_style, feedback), ("UNKNOWN", "未知类型"))
    
    # 匹配职业
    matched = []
    for career, info_c in CAREER_PATHS.items():
        if mbti_code in info_c.get("mbti_preference", []):
            matched.append({"career": career, "description": info_c["description"], "core_skills": info_c["core_skills"]})
    if not matched:
        matched = [{"career": c, "description": CAREER_PATHS[c]["description"], "core_skills": CAREER_PATHS[c]["core_skills"]} for c in CAREER_PATHS]
    
    # 保存到本地档案
    agent_profile.update({
        "did": MY_DID, "llm_type": llm_type, "ati_type": mbti_code, "ati_name": ati_name,
        "career": matched[0]["career"], "created_at": datetime.now().isoformat()
    })
    with open(PROFILE_FILE, "w") as f:
        json.dump(agent_profile, f, ensure_ascii=False)
    
    # 广播
    broadcast_msg = {"type": "mbti_result", "did": MY_DID, "name": MY_NAME, "ati_type": mbti_code, "career": matched[0]["career"], "timestamp": int(time.time())}
    mqtt_client.publish(ATI_RESULT_TOPIC, json.dumps(broadcast_msg).encode())
    
    return jsonify({"ati_type": mbti_code, "ati_name": ati_name, "recommended_careers": matched[:3]})

@app.route("/career/match", methods=["POST"])
def career_match():
    """匹配职业方向"""
    data = request.json or {}
    ati_type = data.get("ati_type") or agent_profile.get("ati_type")
    if not ati_type:
        return jsonify({"error": "需要先完成ATI测试"}), 400
    
    matched = []
    for career, info_c in CAREER_PATHS.items():
        if ati_type in info_c.get("mbti_preference", []):
            matched.append({"career": career, "description": info_c["description"], "core_skills": info_c["core_skills"]})
    if not matched:
        matched = [{"career": c, "description": CAREER_PATHS[c]["description"], "core_skills": CAREER_PATHS[c]["core_skills"]} for c in CAREER_PATHS]
    
    return jsonify({"ati_type": ati_type, "matched_careers": matched[:3]})

@app.route("/mentor/find", methods=["POST"])
def find_mentor():
    """查找Mentor"""
    data = request.json or {}
    career = data.get("career") or agent_profile.get("career")
    if not career:
        return jsonify({"error": "需要先选择职业方向"}), 400
    
    req = {"type": "mentor_request", "did": MY_DID, "name": MY_NAME, "career": career, "timestamp": int(time.time())}
    mqtt_client.publish(MENTOR_DISCOVER_TOPIC, json.dumps(req).encode())
    return jsonify({"status": "searching", "career": career, "note": "等待Mentor响应..."})

@app.route("/kg/capabilities")
def kg_list():
    """列出知识图谱中的能力"""
    with skill_registry_lock:
        caps = [{"name": n, "domain": v.get("domain"), "task": v.get("task"), "success_rate": v.get("success_rate")} for n, v in skill_registry.items()]
    return jsonify({"capabilities": caps, "count": len(caps)})

@app.route("/kg/publish", methods=["POST"])
def kg_publish():
    """发布能力到知识图谱"""
    data = request.json or {}
    cap_name = data.get("capability_name")
    if not cap_name or not data.get("prompt_template"):
        return jsonify({"error": "capability_name和prompt_template不能为空"}), 400
    
    capability = {
        "capability_name": cap_name, "domain": data.get("domain"), "task": data.get("task"),
        "llm_type": data.get("llm_type", "unknown"), "prompt_template": data.get("prompt_template"),
        "success_rate": data.get("success_rate", 0.8), "verified_by": MY_NAME,
        "created_at": datetime.now().isoformat()
    }
    with skill_registry_lock:
        skill_registry[cap_name] = capability
    mqtt_client.publish(KNOWLEDGE_GRAPH_TOPIC, json.dumps(capability).encode())
    return jsonify({"status": "ok", "capability_name": cap_name})

@app.route("/profile")
def get_profile():
    """获取当前Agent档案"""
    return jsonify(agent_profile)

@app.route("/welcome")
def handle_welcome():
    """给新节点的欢迎路标"""
    welcome_md = (Path(__file__).parent / "WELCOME.md").read_text(encoding="utf-8")
    return Response(welcome_md, status=200, content_type="text/markdown; charset=utf-8")

@app.route("/fetch")
def handle_fetch():
    since_str = request.args.get("since", "0")
    try:
        since = int(since_str)
    except:
        since = 0
    msgs = load_recent_messages(200)
    if since > 0:
        msgs = [m for m in msgs if datetime.fromisoformat(m["timestamp"]).timestamp() > since]
    return jsonify({"messages": msgs, "count": len(msgs)})

# ── LLM 代理 ─────────────────────────────────────────────
@app.route("/v1/chat/completions", methods=["POST"])
@app.route("/gateway/v1/chat/completions", methods=["POST"])
def handle_chat_completions():
    req_data = request.json or {}
    skill_name = request.headers.get("X-Echo-Skill-Name", "unknown")
    api_key = request.headers.get("Authorization", "").replace("Bearer ", "")
    if api_key:
        req_data["_api_key"] = api_key
    result, status = forward_request(req_data, skill_name)
    return jsonify(result), status

@app.route("/")
def handle_root():
    return jsonify({"error": "not found"}), 404

# ─── 主程序 ───────────────────────────────────────────────
def main():
    port = 8767
    if "--api" in sys.argv:
        port = int(sys.argv[sys.argv.index("--api") + 1])

    load_or_gen_key()
    init_db()
    _setup_logging()
    global PROVIDERS
    PROVIDERS = load_providers()

    # 从数据库恢复历史到缓存
    history = load_recent_messages(200)
    with cache_lock:
        public_cache.clear()
        public_cache.extend(history)
    logger.info(f"📦 已从数据库恢复 {len(history)} 条消息")

    start_mqtt()

    # 订阅自己的同步响应
    resp_topic = f"echo/private/{MY_DID}/sync_response"
    mqtt_client.subscribe(resp_topic, qos=1)

    # 启动时同步 + 心跳
    broadcast_sync_request()
    threading.Thread(target=broadcast_presence, daemon=True).start()
    threading.Thread(target=cleanup_online, daemon=True).start()

    logger.info(f"🌐 Echo v1.5 话题社区 · 监听 :{port}")
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)

if __name__ == "__main__":
    main()