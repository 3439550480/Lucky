"""
记忆检索注入（06 文档 §3.4）

注入纪律（KV cache 约束）：注入块只出现在提示词动态尾部（三区结构的当前输入区之前）——
固定前缀（05 prefix.py）永不因记忆内容变化。
权衡（06 §7.1）：有相关记忆的请求从记忆块处断开缓存延续；无相关记忆返回空串，缓存照常命中。
职责划分：向量检索在 store（search 接口，换 Qdrant 后端零改动）；本模块只做
「query 向量化 → 调 store.search → 阈值过滤 → 渲染注入块」。
"""
from app.agent.auth.staff_identity import StaffIdentity
from app.conf.app_config import app_config
from app.core.log import logger


def _visible(item, staff: StaffIdentity | None) -> bool:
    """作用域可见性（2.0 上下文策略 2.1/2.3）：
    - personal：仅 owner 本人
    - store：登录即可见（门店层读不设权限点；敏感度在写入侧已门禁）
    - owner_id 为空的存量全局条目：向后兼容，全员可见（升级不丢记忆）"""
    scope = getattr(item, "scope", "personal")
    owner_id = getattr(item, "owner_id", "")
    if scope == "store":
        return True
    if staff is None:
        return True                                   # 评测/离线路径：不设身份，全部可见
    return owner_id in ("", staff.staff_id)


async def retrieve_memory_block(query: str, memory_store,
                                embedding_client, top_k: int | None = None,
                                staff: StaffIdentity | None = None) -> str:
    """按 query 语义检索相关记忆，渲染注入块（纯文本）；无相关记忆返回 ""（不注入空块）。
    [2.0 上下文策略 2.3] 双层作用域：向量检索全量打分后按当前员工可见性过滤
    （个人层=本人 + 门店层=全员），归并取 top_k——单次 search 即完成双路，无需两查"""
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
        ranked = memory_store.search(qvec, top_k * 3)   # 先多取——可见性过滤后再截断
    except Exception as e:
        logger.warning(f"[memory] 记忆检索失败，跳过注入: {e}")
        return ""

    # step 3: 可见性过滤 + 阈值过滤（业务参数，不属于存储层）+ 渲染
    lines = []
    for item, score in ranked:
        if not _visible(item, staff):
            continue                                    # 他人的个人层记忆——直接丢弃
        if score < app_config.memory.similarity_threshold:
            break                                    # 降序排列，后面只会更低
        if hasattr(item, "subject"):                 # MemoryCard：主体+关系+事实聚合渲染
            lines.append(f"- {item.subject}（{item.relation_to_user}）：{'；'.join(item.facts)}")
        else:                                        # SimpleNote：原子事实
            lines.append(f"- {item.content}")
        if len(lines) >= top_k:
            break
    if not lines:
        return ""
    return "【用户已知信息（可自然参考，不要生硬复述）】\n" + "\n".join(lines)
