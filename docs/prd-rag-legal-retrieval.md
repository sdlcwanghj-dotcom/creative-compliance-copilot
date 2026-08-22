# PRD：法规检索 RAG 能力

- 状态：草案 v0.1
- 日期：2026-08-21
- 分支：`feat/langgraph-orchestration`
- 范围：为创意合规副驾（Creative Compliance Copilot）引入基于向量的法规语义检索，并将其接入 LLM 语义分析节点。

## 1. 背景与问题

系统当前已有"两层语料 + 三入口"的检索雏形，但语义能力薄弱：

| 语料 | 文件 | 说明 |
| --- | --- | --- |
| 条款级精编摘要（15 条） | `data/legal_excerpts.json` | 人工整理，带 `keywords`、`interpretation` |
| 法规全文分块 | `data/legal_index.json` | 由 `scripts/build_knowledge_base.py` 从官方 HTML/PDF 抓取切块 |
| 规则→证据绑定（36 条） | `data/rule_evidence.json` | `rule_id → [chunk_id]` 的确定性映射 |

三个使用入口：

1. **审核员自由检索**（`app.py:1148-1167`）：excerpts 走 BM25（`policy_retrieval.search_policy_query`），全文仅 `q in chunk["text"]` 子串匹配。中文近义/同义召不回。
2. **`retrieve_applicable_policy` 图节点**（`app.py:268`、`review_graph.py:133`）：**刻意不做模糊检索**，只返回 `rule_evidence.json` 的确定性绑定（`policy_retrieval.citations_for_findings`）。
3. **LLM 语义节点**（`llm_agent.py:75` `analyze_semantic_risk`）：仅喂 `copy + allowed_rule_ids`，**未注入任何法规原文**，模型凭记忆"盲判"，靠子串校验 + 白名单兜底。

核心问题：入口 1 召回差；入口 3 缺少法条依据，语义判断质量受限。

## 2. 目标与非目标

### 目标
- G1：把审核员自由检索（入口 1）升级为语义/混合检索，显著提升中文近义召回。
- G2：为 LLM 语义节点（入口 3）注入 top-k 法规原文，让模型**基于条文**判断隐含风险——真正意义的"RAG 进 agent 工作流"。
- G3：新增一个共享的本地向量检索模块，供入口 1 与入口 3 复用。

### 非目标
- N1：**不**用 RAG 替换入口 2 的确定性证据绑定。`review_graph.py:9-17` 的设计铁律要求红线规则引用可追溯，不得被相似度分数替代。
- N2：不改动人工裁决流程（不进图，仅 `MemorySaver` 回放分析）。
- N3：不引入外部托管向量库/云检索服务。

## 3. 设计约束（来自现有架构）

- **C1 离线可复现**：`review_graph.py:14-15` 要求无 API key 时降级为可复现的离线流程。因此 embedding **不得**依赖 LLM 端点，须本地计算。
- **C2 确定性优先**：图永不用模型调用替换红线规则（`review_graph.py:10-12`）。RAG 只做"辅助召回"，不做"决策裁定"。
- **C3 证据可追溯**：LLM 产出的每条 violation 仍须 `evidence` 为 copy 子串且 `rule_id ∈ allowed_rule_ids`（`llm_agent.py:84-101`），RAG 注入不放宽该校验。
- **C4 JSON 可序列化状态**：`ReviewState`（`review_graph.py:32`）不得混入不可序列化对象。

## 4. 方案概述

新增本地向量检索模块 `vector_retrieval.py`，对现有两套语料建立向量索引，提供混合检索（BM25 + 向量）。两个入口复用它：

```
                         ┌─────────────────────────┐
data/legal_excerpts.json │  build_vector_index.py  │  → data/vector_index.*
data/legal_index.json ──▶│  (本地 ONNX embedding)   │    (向量 + 元数据)
                         └─────────────────────────┘
                                     │
                     ┌───────────────┴────────────────┐
                     ▼                                 ▼
        入口1 审核员检索(app.py)          入口3 LLM语义节点前(review_graph)
        hybrid_search(query)             retrieve_context(copy) → 注入 prompt
```

### 4.1 embedding 选型

| 方案 | 离线(C1) | 依赖成本 | 结论 |
| --- | --- | --- | --- |
| LLM 端点 embedding API | ❌ 破坏离线 | 低 | 否决 |
| sentence-transformers | ✅ | +torch，包大 | 备选 |
| **ONNX 本地模型 + onnxruntime** | ✅ | 复用已装 `onnxruntime`（requirements.txt:9） | **推荐** |

推荐 ONNX 路线：`onnxruntime` 已在依赖中（配合 `rapidocr`）。选用轻量中文/多语 embedding（如 bge-small-zh 的 ONNX 导出），首次运行下载并缓存到本地。

### 4.2 检索策略

- 向量召回 top-N，与现有 BM25（`search_policy_query`）分数做 **RRF（Reciprocal Rank Fusion）** 融合，避免跨语料分数不可比。
- 保留现有 `min_score` 阈值语义：融合后仍无达阈结果时，沿用当前"不生成弱相关引用"的告警（`app.py:1164-1165`）。

## 5. 详细改动

### 5.1 新增 `vector_retrieval.py`
- `embed(texts) -> list[vector]`：ONNX 推理，带内存缓存。
- `load_index()`：加载 `data/vector_index.*`，缺失时优雅降级（返回空，调用方回退纯 BM25）。
- `vector_search(query, corpus, limit)`：余弦相似 top-k。
- `hybrid_search(query, limit)`：RRF 融合 BM25 + 向量，返回与 `search_policy_query` 兼容的结构（含 `citation_type`、`score`）。

### 5.2 新增 `scripts/build_vector_index.py`
- 读 `legal_excerpts.json` + `legal_index.json`，对每个片段 embedding，落盘 `data/vector_index.json`（或 `.npz`）。
- 记录模型标识与维度，供加载时校验；与 `build_knowledge_base.py` 串联（先建全文再建向量）。

### 5.3 入口 1：审核员检索（`app.py:1148-1167`）
- `search_policy_query` → `hybrid_search`。全文子串匹配替换为向量召回。
- UI 标注检索类型（`bm25` / `vector` / `hybrid`）与融合分。

### 5.4 入口 3：RAG 注入 LLM 语义节点
- `review_graph.py` 新增节点 `retrieve_context`：对 `copy` 调 `hybrid_search`，取 top-k 法规片段写入 `ReviewState.llm_context_snippets`（新增字段，JSON 可序列化，满足 C4）。
- 边：`deterministic_scan` --(需 semantic)--> `retrieve_context` --> `semantic_llm`；不需 semantic 时路由不变（直达 `retrieve_policy`）。改 `_route_after_scan`（`review_graph.py:199`）的目标节点。
- `ReviewDeps` 新增 `retrieve_context_snippets: Callable`（`review_graph.py:57`），由 app 注入，保持图不依赖检索实现。
- `llm_agent.analyze_semantic_risk` 增加可选入参 `context_snippets`，拼入 user payload；system prompt 追加"仅依据 supplied statutes 判断，不得虚构法条"。**C3 的子串 + 白名单校验保持不变。**

### 5.5 入口 2：保持不动
`retrieve_applicable_policy` / `citations_for_findings` / `rule_evidence.json` 一律不改（N1）。若未来需"候选证据"补充，另起字段并显式标注"待人工确认、非权威引用"，不覆盖确定性绑定。

## 6. 数据与配置
- 新增 `data/vector_index.json`（构建产物，随语料重建）。
- `.env` 可选项：`EMBEDDING_MODEL_PATH`、`RAG_TOP_K`（默认 4）、`RAG_MIN_SCORE`。
- `requirements.txt`：确认 `onnxruntime` 版本满足；如选 ONNX 需加分词/下载依赖（如 `tokenizers`、`huggingface_hub`）——按最终模型定。

## 7. 降级与失败路径
- 向量索引缺失/加载失败 → 入口 1、3 均回退纯 BM25，功能不中断。
- ONNX 推理异常 → `retrieve_context` 返回空片段；`semantic_llm` 仍按原逻辑运行（等价于当前"盲判"）。
- 无 API key → 图仍走离线确定性路径，RAG 节点被 `_route_after_scan` 跳过（C1）。

## 8. 测试
- `tests/test_review_graph.py`：新增用例——注入 `retrieve_context_snippets` 桩，断言 semantic 路径下片段进入 LLM 上下文；断言索引缺失时降级不报错。
- `vector_retrieval` 单测：RRF 融合排序、空索引降级、embedding 维度校验。
- `tests/test_regressions.py`：确保入口 2 输出与改动前逐条一致（确定性绑定不受影响）。

## 9. 里程碑
1. `vector_retrieval.py` + `build_vector_index.py` + 单测（可独立验证检索质量）。
2. 入口 1 接入 `hybrid_search`（人机辅助，风险最低，先上线试水）。
3. 入口 3 接入 RAG 注入 + 图节点 + 回归测试。

## 10. 待确认
- Q1：embedding 落 ONNX 还是接受 `sentence-transformers`（换取实现更简单）？
- Q2：首次模型下载能否接受（联网一次）？还是须完全离线、随仓库预置模型文件？
- Q3：入口 1 是否要保留纯 BM25 作为可切换的对照，便于评估召回提升？

