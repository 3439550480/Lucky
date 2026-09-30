"""
Agent 评估模块（03 文档）

包结构：
    dataset.py            评测集加载与校验（§3.5）
    retrieval_metrics.py  检索三通道指标（§3.1，纯函数）
    intent_metrics.py     意图分类指标（§3.2）
    tool_metrics.py       工具/能力触发指标（§3.9）
    sql_metrics.py        SQL 可执行率与结果集正确性（§3.3）
    pricing.py            计费档位判定器（§3.4）
    cost_metrics.py       成本指标（§3.4）
    runner.py             评测执行器（§3.6）
    report.py             报告渲染与对比（§3.7）

设计红线（03 §1.2）：只读最终 state 与 tracker 明细，零侵入主链路；
指标子模块均可经 features.evaluation.* 开关独立关闭（关闭 = 不计算不呈现，绝不输出 0 分）
"""
