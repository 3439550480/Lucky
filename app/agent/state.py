"""
安踏特卖店问数 Agent 状态定义（2.0）

State 是 LangGraph 各节点之间传递和更新的共享数据
本章在用户原始问题之外，新增关键词列表和三路召回结果
并把召回到的实体整理成后续提示词更容易消费的表信息和指标信息
SQL 生成闭环会继续写入候选 SQL 以及校验错误信息，用于控制校正或执行分支
"""

from typing import TypedDict,List, Dict, Any

from app.entities.column_info import ColumnInfo
from app.entities.metric_info import MetricInfo
from app.entities.value_info import ValueInfo


class MetricInfoState(TypedDict):
    """面向 SQL 生成提示词的指标信息"""

    name: str
    description: str
    # 指标依赖的字段 id，用来提示模型不要脱离业务口径随意计算
    relevant_columns: list[str]
    alias: list[str]


class ColumnInfoState(TypedDict):
    """表上下文中的字段信息"""

    name: str
    type: str
    role: str
    # 字段真实样例值，尤其用于辅助 where 条件里的枚举值选择
    examples: list
    description: str
    alias: list[str]


class TableInfoState(TypedDict):
    """SQL 生成阶段真正传给模型的表结构上下文"""

    name: str
    role: str
    description: str
    columns: list[ColumnInfoState]


class DateInfoState(TypedDict):
    """SQL 生成阶段使用的当前日期上下文"""

    date: str
    weekday: str
    quarter: str


class DBInfoState(TypedDict):
    """SQL 生成阶段使用的数据库环境信息"""

    dialect: str
    version: str


class DataAgentState(TypedDict):
    """一次问数链路中的核心状态"""

    query: str  # 用户输入的查询
    keywords: list[str]  # 抽取的关键词
    retrieved_column_infos: list[ColumnInfo]  # 检索到的字段信息
    retrieved_metric_infos: list[MetricInfo]  # 检索到的指标信息
    retrieved_value_infos: list[ValueInfo]  # 检索到的取值信息

    table_infos: list[TableInfoState]  # 合并和补齐后的表结构上下文
    metric_infos: list[MetricInfoState]  # 合并后的指标上下文
    date_info: DateInfoState  # 当前日期 星期和季度信息
    db_info: DBInfoState  # 数据库方言和版本信息

    sql: str  # 生成或校正后的SQL
    # 写入:generate_sql、correct_sql,读取:validate_sql、correct_sql、run_sql.作用:保存候选或修正后的 SQL
    error: str  # 校验SQL时出现的错误信息
    # 写入:validate_sql,读取:graph 条件分支、correct_sql.作用:保存 SQL 校验错误
    # ==== SQL 结果（2.0 上下文策略 1.6.15 拆分）====
    # [规则3 留档] 原版只有 result（完整行数据进 state）——一个值同时服务三个消费者
    # （前端要全量/解释只要前3行/checkpointer 不需要），导致快照膨胀+残留隐患。
    # 拆分：完整结果走 SSE 给前端；state 只留 20 行样本 + 总行数供下游。
    result_sample: list[dict]  # 前 20 行样本（explain_result / 评估消费）
    result_total: int          # 完整结果总行数（与样本数对照，识别截断）
    retry_count: int  # 当前已重试次数
    messages: List[Dict[str,Any]]
    # 对话历史，格式 [{"role": "user", "content": "..."}, {"role": "assistant", "content": "解释或结果"}]
    intent: str          # 意图分类结果
    intent_reply: str    # 意图节点生成的回复（仅用于非查询意图）

    # ==== 能力路由字段（04 文档 §3.5）====
    requested_capability: str  # 前端芯片显式选择（tier-0 输入；空串 = 自动路由）
    capability: str            # 路由选中的能力名（v1: dataquery|default）
    capability_source: str     # 路由来源 user/rules/embedding/llm/fallback/denied（03 评估过滤用）
    tool_calls: list[str]      # v1 由路由写入（03 tool_metrics 数据源；映射见 router._TOOL_MAP）
    # ==== 权限拒绝（2.0 上下文策略 1.4）====
    # [规则3 留档] 首版漏声明这两个字段——LangGraph 合并时丢弃 schema 外字段，
    # 导致拒绝标记丢失、分发仍走到能力链路（冒烟抓出）。字段必须在 State 显式声明
    permission_denied: bool    # 路由层权限校验未通过 → 分发到 permission_denied 终点
    denied_permission: str     # 被拒的权限点（deny 节点渲染用，不透给用户）

    # ==== 2.0 新能力字段（replenish）====
    # [规则3 留档] S3b 设计拍板：inventory 复用问数链路（结果走 result 字段，不设
    # inventory_rows——原预留字段删除）；replenish 是固化算法单节点，结构化建议写这里
    replenish_plan: list[dict]  # 补货建议（replenish_plan 节点写入：固化算法输出 + 参数来源）
