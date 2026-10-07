/**
 * 多会话持久化（Lucky 会话侧栏的数据层）
 *
 * 存储：localStorage
 *   lucky_sessions        → SessionMeta[]（全标签页共享）
 *   lucky_msgs_{id}       → ChatMessage[]（按会话隔离）
 * 活跃会话 id 存 sessionStorage("agent_thread_id")——与 agentApi 的 thread_id 同键同源，
 * 每个标签页可以有自己正在使用的会话（保持 02 文档"thread_id=标签页隔离"语义）
 */
import { uuid } from "./format";
import type { ChatMessage } from "../types/agent";

export interface SessionMeta {
  id: string;          // 即后端 thread_id（会话主键，创建后不可变）
  title: string;       // 首条用户消息截断生成；"新对话" 为未命名占位
  createdAt: number;
  updatedAt: number;
}

const SESSIONS_KEY = "lucky_sessions";
const messagesKey = (id: string) => `lucky_msgs_${id}`;

export function listSessions(): SessionMeta[] {
  try {
    const raw = localStorage.getItem(SESSIONS_KEY);
    const list: SessionMeta[] = raw ? JSON.parse(raw) : [];
    return list.sort((a, b) => b.updatedAt - a.updatedAt);
  } catch {
    return [];   // 存储损坏时降级为空列表（旧数据不强行恢复）
  }
}

export function persistSessions(list: SessionMeta[]): void {
  localStorage.setItem(SESSIONS_KEY, JSON.stringify(list));
}

export function createSessionMeta(title = "新对话"): SessionMeta {
  return { id: uuid(), title, createdAt: Date.now(), updatedAt: Date.now() };
}

export function loadMessages(id: string): ChatMessage[] {
  try {
    return JSON.parse(localStorage.getItem(messagesKey(id)) ?? "[]");
  } catch {
    return [];
  }
}

export function saveMessages(id: string, msgs: ChatMessage[]): void {
  localStorage.setItem(messagesKey(id), JSON.stringify(msgs));
}

export function deleteSessionData(id: string): void {
  localStorage.removeItem(messagesKey(id));
  persistSessions(listSessions().filter((s) => s.id !== id));
}
