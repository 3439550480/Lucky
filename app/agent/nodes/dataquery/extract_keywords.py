"""
关键词抽取节点

负责从用户自然语言问题中识别检索线索
后续字段召回 字段取值召回和指标召回都会基于这些关键词展开
"""

import jieba.analyse
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.session.condition_extractor import extract_conditions, last_user_text
from app.agent.session.context_store import append_user_message
from app.agent.session.history_provider import get_conversation_history
from app.agent.state import DataAgentState
from app.conf.app_config import app_config
from app.core.log import logger


async def extract_keywords(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """抽取用户问题中的关键词，并通过流式输出反馈当前进度"""

    step = "抽取关键词"
    writer = runtime.stream_writer
    writer({"type": "progress", "step": step, "status": "running"})

    try:
        query = state["query"]
        # [05 上下文管理] 写入走 context_store（带 capability/ts 元数据）；
        # [2.0 上下文策略 1.1] 条目级身份 staff_id（鉴权中间件经 Context 注入）
        staff = runtime.context.get("staff")
        if app_config.features.context_management:
            messages = append_user_message(state, query, capability=state.get("capability"),
                                           staff_id=staff.staff_id if staff else None)
        else:
            messages = state.get("messages") or []   # 如果是 None，则初始化为空列表
            messages.append({"role": "user", "content": query})
        # 只保留更可能承载业务含义的词性，减少“的、帮我、一下”这类无检索价值的噪声
        allow_pos = (
            "n",  # 名词: 商品、订单、销售额
            "nr",  # 人名: 张三、李四
            "ns",  # 地名: 华北、北京、上海
            "nt",  # 机构团体名: 门店、品牌、渠道
            "nz",  # 其他专有名词: SKU、GMV、AOV
            "v",  # 动词: 统计、对比、查询
            "vn",  # 名动词: 销售、成交、退款
            "a",  # 形容词: 新增、有效、活跃
            "an",  # 名形词: 可用、有效、异常
            "eng",  # 英文: GMV、SKU、ROI
            "i",  # 成语或习用语，避免遗漏整体表达
            "l",  # 常用固定短语，例如“销售总额”
        )

        # extract_tags 会基于 TF-IDF 抽取关键词，并按 allowPOS 做词性过滤
        keywords = jieba.analyse.extract_tags(query, allowPOS=allow_pos)

        # [P0-3 召回继承] 省略句的召回种子并入继承条件（策略 1.6.9+1.6.12）：
        # "那只看鞋类呢" 实测只召回到品类/补货/流水域列，fact_sales.actual_amount
        # 未召回 → filter_table 把 fact_sales 过滤出表 → SQL 跑偏补货策略表。
        # 【继承条件】块已让模型知道"指标=销售额"，这里让召回也知道——两层对齐才闭环。
        # 失败仅表现为召回偏移（单点可回退），不阻断主流程。
        try:
            prev_text = last_user_text(get_conversation_history(state), query)
            if prev_text:
                inherited_words = [str(v) for k, v in extract_conditions(prev_text).items()
                                   if k != "时间"]      # 时间已由 date_info/继承块承载，不进召回
                if inherited_words:
                    keywords = list(set(keywords + inherited_words))
                    logger.info(f"[keywords] 召回继承并入: {inherited_words}")
        except Exception as e:
            logger.warning(f"[keywords] 召回继承失败（跳过）: {e}")

        # 保留原始问题作为兜底检索入口，避免关键词切分不准时丢掉完整语义
        # set 用来去重；顺序不参与后续判断，所以这里不依赖关键词顺序
        keywords = list(set(keywords + [query]))

        writer({"type": "progress", "step": step, "status": "success"})
        logger.info(f"抽取关键词成功: {keywords}")
        return {"keywords": keywords, "messages": messages}
    except Exception as e:
        logger.error(f"抽取关键词失败: {e}")
        writer({"type": "progress", "step": step, "status": "error"})
        raise