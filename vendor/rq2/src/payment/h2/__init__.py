"""H2 意图-交易对齐验证（Intent-Transaction Alignment Verification）模块。

借鉴来源（机制迁移而非复现，见 文献/MOTIVATION_HOW_REFERENCE_MAPPING.md）：
- 【037】Arbiter：混合验证 DAG——确定性规则节点（目录/参数/环境/receipt 绑定）
  先行、关键失败早停，再由 LLM 语义节点判断意图一致性；可审计的逐节点结论
  与违规类别标签。
- 【069】RTADev：认证链检查点——中间工件（proposal、评价汇总）只有通过
  checkpoint 才能成为下游输入；硬约束由确定性门控而非 LLM 多数意见裁决。
- 【017】：receipt 必须绑定当前状态版本（运行时绑定、防 stale 工件）。

边界（与 H1 分工）：H2 不重新解释自然语言意图（那是 H1），它把对话原文 +
proposal + receipt + 环境事实做独立对齐验证。
"""
