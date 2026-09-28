"""
表信息过滤节点

负责在合并后的候选表结构中筛选出当前问题真正需要的表和字段
这里让大模型只返回“保留哪些表和字段”的选择结果，真正的结构裁剪仍由程序完成
"""

import yaml
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.context import DataAgentContext
from app.agent.state import DataAgentState, TableInfoState
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt


async def filter_table(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """根据用户问题裁剪候选表结构上下文"""

    writer = runtime.stream_writer
    llm = runtime.context["llm"]   # [01 迁移] 用户选择的生成模型（替换 llm 模块级单例）
    step = "过滤表信息"
    writer({"type": "progress", "step": step, "status": "running"})

    try:
        query = state["query"]
        table_infos: list[TableInfoState] = state["table_infos"]

        # table_infos 是嵌套结构，转成 YAML 后更适合放进提示词，也保留中文字段说明
        prompt = PromptTemplate(
            template=load_prompt("filter_table_info"),
            input_variables=["query", "table_infos"],
        )
        # filter_table_info prompt 要求模型只输出 JSON 对象：表名 -> 字段名列表
        output_parser = JsonOutputParser()
        # LCEL 管道：填充提示词 -> 调用模型 -> 解析 JSON
        chain = prompt | llm | output_parser

        result = await chain.ainvoke(
            {
                "query": query,
                "table_infos": yaml.dump(
                    table_infos, allow_unicode=True, sort_keys=False
                ),
            }
        )
        # 模型只负责选择，程序根据选择结果从原始 TableInfoState 中裁剪，避免模型重写复杂结构出错
        # filtered_table_infos: list[TableInfoState] = []
        # for table_info in table_infos:
        #     if table_info["name"] in result:
        #         table_info["columns"] = [
        #             column_info
        #             for column_info in table_info["columns"]
        #             if column_info["name"] in result[table_info["name"]]
        #         ]
        #         filtered_table_infos.append(table_info)
        filtered_table_infos: list[TableInfoState] = []
        for table_info in table_infos:
            if table_info["name"] not in result:
                continue
            needed_columns = result[table_info["name"]]
            kept_columns = [
                column_info
                for column_info in table_info["columns"]
                if column_info["name"] in needed_columns
            ]
            if not kept_columns:
                continue
            # 浅拷贝保留其他元数据（如 comment, database 等）
            filtered_table_info = dict(table_info)
            filtered_table_info["columns"] = kept_columns
            filtered_table_infos.append(filtered_table_info)
        logger.info(
            f"过滤后的表信息：{[filtered_table_info['name'] for filtered_table_info in filtered_table_infos]}"
        )
        writer({"type": "progress", "step": step, "status": "success"})
        return {"table_infos": filtered_table_infos}

    except Exception as e:
        logger.error(f"{step} failed: {e}")
        writer({"type": "progress", "step": step, "status": "error"})
        raise