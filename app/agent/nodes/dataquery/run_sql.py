"""
SQL 执行节点

负责执行最终 SQL，并记录查询结果。
它是当前 SQL 闭环的结束节点，执行完成后流程进入 END。
"""

from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.nodes.common.error_messages import humanize_exec_error
from app.agent.state import DataAgentState
from app.core.log import logger


async def run_sql(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """执行 SQL 并产出最终问数结果"""

    writer = runtime.stream_writer
    step = "执行SQL"
    writer({"type": "progress", "step": step, "status": "running"})

    try:
        # 这里拿到的可能是 generate_sql 直接通过校验的 SQL，也可能是 correct_sql 覆盖后的 SQL
        sql = state["sql"]
        dw_mysql_repository = runtime.context["dw_mysql_repository"]

        # 真实数据库访问统一封装在仓储层，节点只负责从状态取 SQL 并触发执行
        # [2.0 上下文策略 1.6.12] 显式传参（与 replenish 一致）：仓储默认本有
        # 1000 行/30s 保护，此处显式化避免依赖隐式默认
        result = await dw_mysql_repository.run(sql, timeout_ms=30000, max_rows=1000)
        logger.info(f"SQL执行结果：{len(result)} 行")
        writer({"type": "progress", "step": step, "status": "success"})
        writer({"type": "result", "data": result})   # 完整结果只走 SSE 给前端
        # [2.0 上下文策略 1.6.15] state 只留样本：LLM 从不需要看完整结果
        # （它只要形状+前几行+聚合值；完整结果给前端，两者走不同通道）
        return {"result_sample": result[:20], "result_total": len(result)}

    except Exception as e:
        # [v1.1 FR-07 重写留档] 原版：logger 记录原始异常后直接 raise —— pymysql 内部
        # 报错（表/字段/路径细节）经 QueryService 的 str(e) 原样推给前端，违反 FR-07
        # "禁止输出堆栈与内部路径"。修复：源头人话化后重抛净化异常，日志仍留全量原文
        # 供排查（两处分工：日志面向开发者，SSE 面向用户）。
        friendly = humanize_exec_error(e)
        logger.error(f"{step} failed: {e}")   # 完整异常只进日志
        writer({"type": "progress", "step": step, "status": "error"})
        raise RuntimeError(friendly) from e   # 重抛净化文本，控制流与原版一致