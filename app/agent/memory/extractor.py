"""
记忆提取（06 文档 §3.3 v1.1，单通道设计）

链路位置：QueryService finally 阶段（响应已发送完毕后执行，不阻塞用户）。
设计要点（2026-10-02 修订，正则预筛已废弃——理由见 06 §3.3 修订注记）：
  1. 单通道 LLM 上下文压缩：读当前轮 query+answer，按 prompt 四条硬约束
     （原子化/规范化/值保真/确定性）压缩为规范化事实语料
  2. 内容级去重：写入前对照 store 已有内容集合，同内容跳过（重复陈述不累积）
  3. 条目 embedding 随写随存（失败置 None 不阻断）
  4. 提取/写入/向量化失败只打 warning，绝不影响主链路；LLM 不可用时本轮跳过
     （已存记忆的检索不受影响——检索不依赖提取 LLM）
成本归属：提取链 with_config(run_name="memory_extract") → tracker by_stage 正确归属（§5）
"""
import time

from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import PromptTemplate

from app.agent.memory.store import MemoryCard, MemoryStore, SimpleNote, new_id
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt


def _strip_json_fence(text: str) -> str:
    """剥离 LLM 可能包裹的 ```json 代码块（JsonOutputParser 对裸 JSON 更稳）"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```")
        text = text.removesuffix("```")
    return text.strip()


async def _embed(text: str, embedding_client) -> list | None:
    """单条文本向量化（失败返回 None 不阻断——vector 缺失的条目检索时跳过）"""
    if embedding_client is None:
        return None
    try:
        return await embedding_client.aembed_query(text)
    except Exception as e:
        logger.warning(f"[memory] 向量化失败（该条目检索时将跳过）: {e}")
        return None


async def extract_memories(query: str, answer: str, store: MemoryStore,
                           llm, tracker, embedding_client,
                           thread_id: str) -> dict:
    """运行后提取入口（06 §3.3 v1.1）。返回 {"notes_added": int, "cards_added": int}"""
    notes_added = cards_added = 0
    try:
        # step 1: LLM 上下文压缩（单通道，run_name 使 tracker by_stage 归属 memory_extract）
        chain = (
            PromptTemplate(
                template=load_prompt("memory_extract"),
                input_variables=["query", "answer"],
            )
            | llm
            | JsonOutputParser()
        ).with_config(run_name="memory_extract")
        raw = await chain.ainvoke(
            {"query": query, "answer": answer},
            config={"callbacks": [tracker]} if tracker else None,
        )
        result = _strip_json_fence(raw) if isinstance(raw, str) else (raw or {})

        # step 2: 内容级去重集合（跨 note/card 的已有内容；重复陈述不累积）
        existing = {n.content for n in store.all_notes()}
        existing |= {f for c in store.all_cards() for f in c.facts}

        # step 3: 写入 SimpleNotes（向量化 → 落库；单条失败不阻断批次）
        for fact in (result.get("notes") if isinstance(result, dict) else None) or []:
            fact = (fact or "").strip()
            if not fact or fact in existing:
                continue
            vector = await _embed(fact, embedding_client)
            store.save_note(SimpleNote(
                id=new_id(), content=fact, ts=time.time(),
                source_thread_id=thread_id, vector=vector))
            existing.add(fact)
            notes_added += 1

        # step 4: 写入 MemoryCards（向量取 subject+facts 的联合语义；残缺卡片不落库）
        for card in (result.get("cards") if isinstance(result, dict) else None) or []:
            card = card or {}
            subject = (card.get("subject") or "").strip()
            facts = [f.strip() for f in (card.get("facts") or []) if (f or "").strip()]
            if not subject or not facts:
                continue
            new_facts = [f for f in facts if f not in existing]
            if not new_facts:
                continue                      # 全部事实都已存在 → 不重复建卡
            vector = await _embed(subject + "；" + "；".join(new_facts), embedding_client)
            store.save_card(MemoryCard(
                id=new_id(), subject=subject,
                relation_to_user=card.get("relation_to_user", ""),
                facts=new_facts, narrative=card.get("narrative", ""),
                ts=time.time(), source_thread_id=thread_id, vector=vector))
            existing.update(new_facts)
            cards_added += 1
    except Exception as e:
        # 失败语义（06 §1.1/验收标准 3）：只 warning 不抛，绝不影响主链路；
        # LLM 不可用时本轮跳过，已存记忆的检索不受影响
        logger.warning(f"[memory] 记忆提取失败（本轮跳过，已存记忆检索不受影响）: {e}")

    if notes_added or cards_added:
        logger.info(f"[memory] 提取完成: notes={notes_added} cards={cards_added}")
    return {"notes_added": notes_added, "cards_added": cards_added}
