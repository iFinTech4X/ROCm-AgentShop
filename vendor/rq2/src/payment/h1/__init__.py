"""H1 结构化意图表达（Structured Intent）模块。

借鉴来源（见 文献/MOTIVATION_HOW_REFERENCE_MAPPING.md，均为机制迁移而非复现）：
- 【048】Tagliaferro：分阶段流水线（抽取 → 可验证性分类 → 形式化），语法正确
  ≠ 语义忠实；阶段间可追踪。
- 【032】Rastogi SGD：先定 scheme/场景，再按场景确定必需字段与执行条件。
- 【012】Wang AwN：问题分类法 IMKI/IMR/IwE/IBTC 映射为字段状态
  missing/ambiguous/invalid/unsupported；执行受阻的不确定性才触发澄清
  （fail-closed）。
- 【013】Suri：不确定性定义在参数字段上，而非整句置信度。
"""
