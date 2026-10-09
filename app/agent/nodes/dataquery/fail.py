from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.nodes.common.error_messages import build_fail_reply
from app.agent.state import DataAgentState
from app.core.log import logger


async def fail(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """超过重试次数，返回失败信息。

    [v1.1 FR-07 重写留档] 原版：`SQL 经过 N 次修正仍无法通过校验，请稍后重试。`
    缺陷：仅"发生了什么"无原因与建议，不符合 PRD FR-07 三类信息要求；
    修复：build_fail_reply 生成三段式文案（含脱敏表名摘要，禁 SQL 原文）。
    """
    writer = runtime.stream_writer
    writer({"type": "progress", "step": "SQL校验失败", "status": "error"})
    error_msg = build_fail_reply(state)
    logger.error(f"SQL 校验重试耗尽（FR-07 人话文案已推送）: {state.get('error')}")
    writer({"type": "error", "message": error_msg})
    return {}