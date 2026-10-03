"""
记忆检索注入（06 文档 §3.4）

注入纪律（KV cache 约束）：注入块只出现在提示词动态尾部（三区结构的当前输入区之前）——
固定前缀（05 prefix.py）永不因记忆内容变化。
权衡（06 §7.1）：有相关记忆的请求从记忆块处断开缓存延续；无相关记忆返回空串，缓存照常命中。
职责划分：向量检索在 store（search 接口，换 Qdrant 后端零改动）；本模块只做
「query 向量化 → 调 store.search → 阈值过滤 → 渲染注入块」。
"""
from app.conf.app_config import app_config
from app.core.log import logger


async def retrieve_memory_block(query: str, memory_store,
                                embedding_client, top_k: int | None = None) -> str:
    """按 query 语义检索相关记忆，渲染注入块（纯文本）；无相关记忆返回 ""（不注入空块）"""
    if memory_store is None:
        return ""
    top_k = top_k or app_config.memory.retrieval_top_k

    # step 1: query 向量化（失败 → 放弃注入，不阻断主链路）
    try:
        qvec = await embedding_client.aembed_query(query)
    except Exception as e:
        logger.warning(f"[memory] 查询向量化失败，跳过记忆注入: {e}")
        return ""

    # step 2: store 向量检索（余弦在存储层完成，换后端零改动）
    try:
        ranked = memory_store.search(qvec, top_k)
    except Exception as e:
        logger.warning(f"[memory] 记忆检索失败，跳过注入: {e}")
        return ""

    # step 3: 阈值过滤（业务参数，不属于存储层）+ 渲染
    lines = []
    for item, score in ranked:
        if score < app_config.memory.similarity_threshold:
            break                                    # 降序排列，后面只会更低
        if hasattr(item, "subject"):                 # MemoryCard：主体+关系+事实聚合渲染
            lines.append(f"- {item.subject}（{item.relation_to_user}）：{'；'.join(item.facts)}")
        else:                                        # SimpleNote：原子事实
            lines.append(f"- {item.content}")
    if not lines:
        return ""
    return "【用户已知信息（可自然参考，不要生硬复述）】\n" + "\n".join(lines)
