import yaml
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.session.history_provider import get_conversation_history, render_history
from app.agent.state import DataAgentState
from app.conf.app_config import app_config
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt


async def explain_result(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """根据用户问题、SQL、查询结果生成自然语言解释"""
    writer = runtime.stream_writer
    llm = runtime.context["llm"]   # [01 迁移] 用户选择的生成模型（替换 llm 模块级单例）
    # [规则3 留档] 课件原版：writer("生成结果解释") —— 裸字符串违反 00 §4.2 事件协议
    # （非 dict 会让 QueryService/前端 .get 崩溃），修复为标准 progress 事件
    writer({"type": "progress", "step": "生成结果解释", "status": "running"})

    query = state["query"]
    sql = state["sql"]
    result = state.get("result", [])  # 需要在 run_sql 节点中写入 result
    metric_infos = state.get("metric_infos", [])

    # 如果没有结果（可能因为异常），跳过解释
    if not result:
        writer({"type": "explanation", "text": "没有查询到结果，无法生成解释。"})
        return {}

    # 准备上下文：取结果前3行作为样例
    sample_result = result[:3]
    result_yaml = yaml.dump(sample_result, allow_unicode=True, sort_keys=False)
    metric_yaml = yaml.dump(metric_infos, allow_unicode=True, sort_keys=False)

    # [05 上下文管理] 开关二选一（新三区 / legacy 混排）
    if app_config.features.context_management:
        from app.agent.session.prefix import build_system_prefix
        prompt = PromptTemplate(
            template=load_prompt("explain_result"),
            input_variables=["system_prefix", "conversation_history",
                             "query", "sql", "result", "metric_infos"],
        )
        chain = prompt | llm | StrOutputParser()
        explanation = await chain.ainvoke({
            "system_prefix": build_system_prefix(),
            # [2.0 上下文策略] 历史渲染统一走 render_history（剥离元数据）
            "conversation_history": render_history(
                get_conversation_history(state), "yaml"),
            "query": query,
            "sql": sql,
            "result": result_yaml,
            "metric_infos": metric_yaml,
        })
    else:
        prompt = PromptTemplate(
            template=load_prompt("legacy/explain_result"),
            input_variables=["query", "sql", "result", "metric_infos"],
        )
        chain = prompt | llm | StrOutputParser()
        explanation = await chain.ainvoke({
            "query": query,
            "sql": sql,
            "result": result_yaml,
            "metric_infos": metric_yaml,
        })
    logger.info(f"解释结果：{explanation}")
    writer({"type": "explanation", "text": explanation})
    writer({"type": "progress", "step": "生成结果解释", "status": "success"})

    # 保存助手消息到历史 —— [05] 写入走 context_store（带 capability 元数据）
    if app_config.features.context_management:
        from app.agent.session.context_store import append_assistant_message
        messages = append_assistant_message(state, explanation, capability=state.get("capability"))
    else:
        messages = state.get("messages", [])
        messages.append({"role": "assistant", "content": explanation})
    return {"messages": messages}