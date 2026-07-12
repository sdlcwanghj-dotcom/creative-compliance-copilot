# Creative Compliance Copilot

**AI-assisted Ad Review and Policy Retrieval System**  
**广告素材智能预审与审核员辅助系统**

面向大型广告平台内部审核场景的可运行 MVP。主要用户是广告审核员和风控运营人员。系统是审核后台中的 AI 辅助模块，不声称复刻字节、抖音或其他公司的内部审核系统。

## 业务边界

- AI 负责素材解析、风险识别、政策检索、证据定位、相似案例召回和整改建议。
- 禁用词、文件规格、价格一致性等确定性检查由代码和规则引擎完成。
- 最终通过、驳回、要求整改和升级复核由人工审核员确认。
- MVP 仅覆盖美妆/护肤品、广告文案和单张图片，不处理完整视频。
- 不自动封禁广告或账户，不计算处罚、金额、权限、CTR、预算或定向。

## 主流程

```text
广告主提交素材
→ RapidOCR 提取图片文字
→ 识别美妆行业和商品类型
→ 确定性规则检查
→ 规则 evidence chunk 确定性引用
→ BM25 法规检索
→ 规则语义检查 / 可选 LLM Agent 语义分析
→ SQLite 相似案例召回
→ Pydantic 结构化风险报告
→ 合规整改文案
→ 审核员人工裁决
→ SQLite 工单状态与审计日志
```

## 页面

- **审核工作台**：默认首页。内置 30 条模拟任务，可按状态、优先级和审核队列筛选，并查看 SLA、广告位、素材类型、审核员和机器结论；在三栏审核台中完成素材核验、风险分析、政策/案例检索和人工裁决。
- **机器预审**：上传单张图片和文案，运行 OCR、规则检查、政策引用、相似案例检索和结构化报告导出。
- **规则中心**：检索 23 条 MVP 执行规则、公开法规原文和本地归档；自由查询使用 BM25。
- **质检评测**：实际运行 24 条固定样本并计算指标，不展示预填占位数据。

## 数据与持久化

- `data/rules.json`：扩展规则目录；MVP 激活其中 23 条美妆/通用规则。
- `data/tickets.json`：30 条模拟生产任务，覆盖 13 个审核队列和完整状态流转。
- `data/cases.json`：40 条公开语境下自建的合规与违规历史案例。
- `data/evaluation_cases.json`：24 条带人工标签的固定评测样本。
- `data/rule_evidence.json`：执行规则到公开法规片段的显式引用关系。
- `data/legal_sources/`：已归档的官方法规页面。
- `data/legal_index.json`：65 个本地法规检索片段。
- `data/legal_excerpts.json`：条款级检索摘要。
- `data/review.db`：SQLite 历史案例、当前工单状态和人工审核日志。

所有平台规则和历史案例均为公开规范转译或模拟数据，不来自任何真实公司的内部系统。

## 运行

```powershell
pip install -r requirements.txt
python scripts\build_knowledge_base.py
streamlit run app.py --server.port 8502
```

打开 `http://127.0.0.1:8502`。

演示审核员账号：

```text
账号：reviewer.a
口令：demo123
```

也可使用 `reviewer.b` 或 `risk.ops`。演示口令可通过环境变量 `DEMO_REVIEW_PASSWORD` 覆盖；生产环境应替换为企业 SSO/OIDC 和服务端权限校验。

## 执行模式

系统默认运行**离线确定性流程**：规则检查、确定性法规引用、BM25 检索、OCR、相似案例和受约束改写均可在无 API Key 时运行。界面会明确显示当前模式，不会把固定流程冒充为 LLM Agent。

需要演示真实模型规划和语义分析时，编辑项目根目录的 `.env`：

```dotenv
LLM_API_KEY=your-api-key
LLM_MODEL=your-model-name
LLM_API_BASE=https://api.openai.com/v1
LLM_TIMEOUT_SECONDS=30
```

保存后重启 Streamlit。`.env` 已加入 `.gitignore`，不会被 Git 提交；`.env.example` 是不含真实密钥的配置模板。系统环境变量优先于 `.env`。

也可以只为当前 PowerShell 会话临时配置：

```powershell
$env:LLM_API_KEY="your-key"
$env:LLM_MODEL="your-model"
$env:LLM_API_BASE="https://api.openai.com/v1"
streamlit run app.py --server.port 8502
```

启用后，模型负责选择审核工具并补充隐含语义风险；禁用词、行业选择、文件规格、资质、价格一致性和最终人工裁决仍由确定性代码控制。模型异常时自动降级，并在工具轨迹中展示降级状态。

政策引用分为两类：规则命中只返回 `data/rule_evidence.json` 中明确绑定的法规片段，不计算伪相似度；审核员自由查询使用 BM25。找不到达到阈值的依据时返回“未找到可引用依据”。

## 验收演示

1. 登录后在“审核工作台”直接点击任务表格中的 `AR-0712-1042`，查看风险高亮、政策引用、相似案例和审核流程状态。
2. 勾选人工确认，选择通过、驳回、要求整改或升级复核，随后在“审核日志回放”查看保存记录。
3. 打开 `AR-0712-1018` 演示合规素材。
4. 打开 `AR-0712-0954` 或在“机器预审”输入医疗化文案，演示高风险素材强制人工复核。
5. 在“机器预审”上传包含文字的图片，查看 RapidOCR 结果、视觉规格检查和结构化 JSON 报告。

## 评测指标

当前页面逐条运行 24 条自建美妆样本。当前离线规则版本的实测结果为：违规召回率 100%、合规素材误报率 0%、风险片段定位率 92.6%、公开法规引用覆盖率 72.5%、改写残留规则率 0%、人工升级判断准确率 75%、结构化字段完整率 100%。这些是本地固定样本结果，不代表线上生产效果；修改规则后页面会重新计算。

## 免责声明

本项目不构成法律意见。法规适用性、规则版本和最终审核结论应由具备权限的合规人员确认。详细来源见 [REFERENCES.md](REFERENCES.md)。
