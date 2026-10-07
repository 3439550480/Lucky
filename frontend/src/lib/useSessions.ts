/**
 * 多会话状态 hook（Lucky 会话侧栏）
 *
 * 职责：会话列表状态 + 活跃会话切换 + 消息装载/持久化 + thread_id 同步
 * 不变量：session.id 即后端 thread_id，创建后不可变（05 checkpointer 按 thread_id 恢复会话）
 */
import { useCallback, useMemo, useState } from "react";
import type { ChatMessage } from "../types/agent";
import {
  createSessionMeta,
  deleteSessionData,
  listSessions,
  loadMessages,
  persistSessions,
  saveMessages,
  type SessionMeta,
} from "./sessionStore";
import { setThreadId } from "./agentApi";

const ACTIVE_KEY = "agent_thread_id";

/** 初始化（useMemo 单次执行；副作用幂等——重复执行读到已有会话不再新建） */
function bootstrap() {
  const sessions = listSessions();
  let id = sessionStorage.getItem(ACTIVE_KEY) ?? "";
  if (!id || !sessions.some((s) => s.id === id)) {
    // 无活跃会话（首次打开/被删/异端残留）→ 就近复用最近会话，没有则新建
    if (sessions.length > 0) {
      id = sessions[0].id;
    } else {
      const meta = createSessionMeta();
      sessions.unshift(meta);
      persistSessions(sessions);
      id = meta.id;
    }
    sessionStorage.setItem(ACTIVE_KEY, id);
  }
  return { sessions, activeId: id, messages: loadMessages(id) };
}

export function useSessions() {
  const initial = useMemo(bootstrap, []);
  const [sessions, setSessions] = useState<SessionMeta[]>(initial.sessions);
  const [activeId, setActiveId] = useState<string>(initial.activeId);
  const [messages, setMessages] = useState<ChatMessage[]>(initial.messages);

  /** 消息更新（自动随写随存当前会话）——App 的 onEvent 改调此函数 */
  const updateMessages = useCallback(
    (updater: ChatMessage[] | ((prev: ChatMessage[]) => ChatMessage[])) => {
      setMessages((prev) => {
        const next = typeof updater === "function" ? updater(prev) : updater;
        saveMessages(activeId, next);
        return next;
      });
    },
    [activeId],
  );

  /** 触活会话（发消息时调用；首条用户消息生成会话标题） */
  const touchSession = useCallback(
    (maybeTitle?: string) => {
      setSessions((prev) => {
        const next = prev
          .map((s) =>
            s.id === activeId
              ? {
                  ...s,
                  updatedAt: Date.now(),
                  title: maybeTitle && s.title === "新对话" ? maybeTitle.slice(0, 24) : s.title,
                }
              : s,
          )
          .sort((a, b) => b.updatedAt - a.updatedAt);
        persistSessions(next);
        return next;
      });
    },
    [activeId],
  );

  /** 新建会话：元数据落库 → thread_id 同步 → 消息清空 */
  const newSession = useCallback(() => {
    const meta = createSessionMeta();
    const next = [meta, ...listSessions()];
    persistSessions(next);
    setSessions(next);
    sessionStorage.setItem(ACTIVE_KEY, meta.id);
    setThreadId(meta.id);
    setActiveId(meta.id);
    setMessages([]);
  }, []);

  /** 切换会话：thread_id 同步 → 装载目标会话消息 */
  const switchTo = useCallback(
    (id: string) => {
      if (id === activeId) return;
      sessionStorage.setItem(ACTIVE_KEY, id);
      setThreadId(id);
      setActiveId(id);
      setMessages(loadMessages(id));
    },
    [activeId],
  );

  /** 删除会话；删的是活跃会话则就近切换，列表空则新建 */
  const deleteSession = useCallback(
    (id: string) => {
      deleteSessionData(id);
      const rest = listSessions();
      if (id !== activeId) {
        setSessions(rest);
        return;
      }
      if (rest.length > 0) {
        const target = rest[0];
        sessionStorage.setItem(ACTIVE_KEY, target.id);
        setThreadId(target.id);
        setSessions(rest);
        setActiveId(target.id);
        setMessages(loadMessages(target.id));
      } else {
        const meta = createSessionMeta();
        const next = [meta];
        persistSessions(next);
        sessionStorage.setItem(ACTIVE_KEY, meta.id);
        setThreadId(meta.id);
        setSessions(next);
        setActiveId(meta.id);
        setMessages([]);
      }
    },
    [activeId],
  );

  return { sessions, activeId, messages, updateMessages, touchSession, newSession, switchTo, deleteSession };
}
