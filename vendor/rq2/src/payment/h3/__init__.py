"""H3 动态决策边界与人机协同模块。

借鉴来源（机制迁移而非复现，见 文献/MOTIVATION_HOW_REFERENCE_MAPPING.md）：
- 【062】ICC-Bandit：上下文 + 约束 + 反馈的在线选择抽象——本模块离线适配为
  确定性的上下文边界策略（不实现 bandit 训练）。
- 【074】：慢环（边界配置）/快环（事件触发检查）双时间尺度。
- 【012】【013】：按字段不确定性（H1 提供）与执行阻碍决定澄清；fail-closed。
- 【017】：状态版本绑定——EXECUTE 的 receipt 必须绑定当前状态版本。

边界规则（确定性、可审计，绝不把 EXECUTE 升级为其他行为的逆操作也从不做
"无条件放行"）：
  自动执行区：意图完备 + 无 H2 硬门否决 + 环境硬事实允许 + receipt 绑定当前状态。
  观察/补证区：EXECUTE 缺当前版本 receipt 且预算未用完 -> 模块自行补做一次
    模拟（H3 re-verification，receipt 标记 source=h3_policy）。
  人工/阻止区：风险越界（risk_score >= threshold）、无授权、余额不足、
    路由不可用 -> BLOCK；意图必需字段缺失 -> REQUEST_CLARIFICATION；无法补证
    （预算耗尽）的 EXECUTE -> BLOCK。
永不把 BLOCK/REQUEST_CLARIFICATION 升级为 EXECUTE（fail-closed）。
"""
