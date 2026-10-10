"""
SQL 生成节点

负责根据用户问题和前面整理出的表结构 指标 日期 数据库环境生成候选 SQL。
本节点只生成 SQL，不做校验和执行，后续会交给 validate_sql 和 run_sql 继续处理。
"""

import yaml
import datetime

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.session.condition_extractor import build_inherited_block
from app.agent.session.history_provider import get_conversation_history
from app.agent.state import DataAgentState
from app.conf.app_config import app_config
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt


async def generate_sql(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """基于已检索和过滤的上下文生成 SQL"""

    writer = runtime.stream_writer
    llm = runtime.context["llm"]   # [01 迁移] 用户选择的生成模型（替换 llm 模块级单例）
    step = "生成SQL"
    writer({"type": "progress", "step": step, "status": "running"})

    try:
        # 这些上下文都由前置节点准备完成，模型只在给定表 字段 指标口径范围内生成 SQL
        table_infos = state["table_infos"]
        metric_infos = state["metric_infos"]
        date_info = state["date_info"]
        db_info = state["db_info"]
        query = state["query"]

        # [05 上下文管理] 历史读取与提示词模板按开关二选一：
        # 开 = history_provider 全量 + 三区结构（前缀稳定，KV cache 可命中）
        # 关 = 现状行为（最近 10 条 + legacy 混排模板）——对照实验基线
        if app_config.features.context_management:
            # [2.0 上下文策略 1.6.9] 条件提取替代整段历史：generate_sql 只需要
            # "条件与实体"（在 user query 里），不需要助手的数字长回复——
            # 结构化继承块 + 最近 1 轮原文兜底，缓存更友好、token 更省
            try:
                today = datetime.date.fromisoformat(str(date_info.get("date", ""))[:10])
            except Exception:
                today = None
            inherited = build_inherited_block(
                query, get_conversation_history(state), today)
            template = load_prompt("generate_sql")
            input_variables = ["system_prefix", "inherited_conditions", "memory_block",
                               "table_infos", "metric_infos", "date_info", "db_info", "query"]
            # [06 记忆注入] 动态尾部（对话历史之后、本次上下文之前，06 §3.4 纪律）；
            # long_term 关闭/无记忆/检索失败 → 空串（模板结构不变，缓存照常命中）
            memory_block = ""
            if app_config.features.memory.long_term:
                try:
                    from app.agent.memory.retriever import retrieve_memory_block
                    memory_block = await retrieve_memory_block(
                        query, runtime.context.get("memory_store"),
                        runtime.context["embedding_client"],
                        staff=runtime.context.get("staff"))
                except Exception as e:
                    logger.warning(f"[memory] 记忆注入失败（跳过）: {e}")
        else:
            # [2.0 P0 修复留档] 原版此分支算出 history_yaml 却从未使用，且 chain_input
            # 仍无条件注入 inherited_conditions → 关开关即 NameError，基线组跑不起来。
            # 基线语义：忠实复现"策略生效前"的输入（最近 10 条原文，元数据不剥离）
            history_yaml = yaml.dump(
                (state.get("messages") or [])[-10:], allow_unicode=True, sort_keys=False)
            template = load_prompt("legacy/generate_sql")
            input_variables = ["table_infos", "metric_infos", "date_info",
                               "db_info", "query", "conversation_history"]
            memory_block = ""

        prompt = PromptTemplate(template=template, input_variables=input_variables)
        # SQL 生成节点只需要纯文本 SQL，不能要求模型输出 JSON 或 Markdown 代码块
        output_parser = StrOutputParser()
        chain = prompt | llm | output_parser

        # step 1: 公共上下文（两分支共用；与占位符一一对应——多传无害、少传必崩）
        chain_input = {
            # YAML 更适合放进提示词：保留嵌套结构 顺序和中文说明，方便模型理解表字段关系
            "table_infos": yaml.dump(
                table_infos, allow_unicode=True, sort_keys=False
            ),
            "metric_infos": yaml.dump(
                metric_infos, allow_unicode=True, sort_keys=False
            ),
            "date_info": yaml.dump(date_info, allow_unicode=True, sort_keys=False),
            "db_info": yaml.dump(db_info, allow_unicode=True, sort_keys=False),
            "query": query,
        }
        # step 2: 分支专属占位符按开关注入（修复点）——开关开的组：三区前缀 + 继承条件块
        # + 记忆块；开关关的组：legacy 模板要的对话历史原文
        if app_config.features.context_management:
            from app.agent.session.prefix import build_system_prefix
            chain_input["system_prefix"] = build_system_prefix(runtime.context.get("staff"))
            chain_input["inherited_conditions"] = inherited
            chain_input["memory_block"] = memory_block
        else:
            chain_input["conversation_history"] = history_yaml

        result = await chain.ainvoke(chain_input)
        logger.info(f"生成的SQL：{result}")
        writer({"type": "progress", "step": step, "status": "success"})
        return {"sql": result}

    except Exception as e:
        logger.error(f"{step} failed: {e}")
        writer({"type": "progress", "step": step, "status": "error"})
        raise