# 变更日志

本项目所有值得记录的改动都会写在这个文件里。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [未发布]

### 2026-08-21：混合检索与 RAG 注入

#### 新增
- 新增混合检索模块（`vector_retrieval.py`）：`hybrid_search` 合并 BM25 与向量相似度，未配置本地向量模型时自动降级为 BM25，保持离线可复现。
- 新增向量索引构建脚本（`scripts/build_vector_index.py`）：将条款摘要嵌入为 `data/vector_index.json`；未设置 `EMBEDDING_MODEL_PATH` 时不写索引并提示降级。
- LLM 语义分析节点接入 RAG：调用模型前通过 `retrieve_context_snippets` 召回候选法规原文并注入 prompt，作为召回上下文提升隐含风险判断。

#### 变更
- 审核员自由查询（入口 1）由 BM25 + 子串匹配改为 `hybrid_search` 混合检索，保留原有全部字段。
- RAG 注入不改变规则命中的确定性引用（`rule_evidence.json`）；语义命中仍受 `allowed_rule_ids` 白名单和原文子串校验约束。
- `README.md` 补充混合检索/RAG 的构建步骤（`EMBEDDING_MODEL_PATH`、`RAG_TOP_K`）与数据文件说明。

### 2026-08-21：LangGraph 编排

#### 新增
- 用 LangGraph 状态图（`review_graph.py`）编排审核流程，依赖通过 `ReviewDeps` 注入，使编排逻辑与 Streamlit 解耦、可独立测试。
- 新增图分支回归测试（`tests/test_review_graph.py`），覆盖：离线路径跳过 LLM 保持确定性、`use_llm=False` 短路、语义命中合并并升级、规划节点门控掉语义节点、规划失败降级为确定性流程。

#### 变更
- `run_review_agent` 改为 LangGraph 图的薄适配层，对外调用签名不变。
- LLM「工具规划」步骤现在真正门控语义分析节点：未被选中则跳过该节点；模型异常时图自动降级回离线确定性流程。
- `README.md` 按现状更新主流程、执行模式与回归测试说明。

#### 依赖
- 声明 `langgraph>=0.2`。

### 更早（已记录于 git 历史）
- 声明 Pydantic 依赖。
- 改用 Trixie GLib 运行时包。
- 配置 Streamlit Cloud 部署，并从 config.toml 移除 port 设置。
