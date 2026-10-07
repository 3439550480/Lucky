/**
 * 智能体接口客户端
 * 封装后端 /api/query SSE 流式接口请求与事件解析逻辑，
 * 以及模型列表 / 能力芯片列表的获取（02 文档 §3.4）。
 */
import type { AgentEvent, CapabilitiesResponse, ModelsResponse } from "../types/agent";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, "") ?? "";

// 调用 streamQuery 时能传的参数：
// signal：一个「取消令牌」。用户点「停止」时，你把它 abort，请求就会中断。
// model / capability：用户选的模型和能力，可以不选。
// onEvent：回调函数。每收到一个后端事件，就调用它，把事件交给上层。这是这套设计的核心——客户端不关心事件怎么渲染，只负责「翻译并转交」。
type QueryOptions = {
  signal?: AbortSignal;
  model?: string;        // 用户选的 LLM provider；未定义时请求体不含该字段
  capability?: string;   // 芯片显式选择（tier-0）；未定义时走自动路由
  onEvent: (event: AgentEvent) => void;
};

// [修复3] 判定"用户主动停止"——与真实网络错误区分，调用方（App.tsx）据此
// 展示"已停止"而非报错气泡。abort 可能在 fetch 阶段或 reader.read 阶段抛出，
// 统一用这个判定函数，调用方不需要理解 DOMException 细节
export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

// 获取当前会话的 thread_id（存储在 sessionStorage 中）
function getThreadId(): string {
  let threadId = sessionStorage.getItem("agent_thread_id");
  if (!threadId) {
    threadId = crypto.randomUUID();
    sessionStorage.setItem("agent_thread_id", threadId);
  }
  return threadId;
}

// 重置会话（新对话时调用）
export function resetThreadId(): void {
  sessionStorage.removeItem("agent_thread_id");
}

// [Lucky 多会话] 显式设置活跃会话的 thread_id（useSessions 切换/新建会话时调用）；
// 与 getThreadId 同键——设置后 getThreadId 直接命中，SSE 继续该会话的后端上下文
export function setThreadId(id: string): void {
  sessionStorage.setItem("agent_thread_id", id);
}

// [NEW] 获取可用模型列表（后端从配置读取，前端零硬编码）
export async function fetchModels(signal?: AbortSignal): Promise<ModelsResponse> {
  const response = await fetch(`${API_BASE_URL}/api/models`, { signal });
  if (!response.ok) {
    throw new Error(`获取模型列表失败：HTTP ${response.status}`);
  }
  return (await response.json()) as ModelsResponse;
}

// [NEW] 获取可选能力芯片列表（selectable=true 的能力）
export async function fetchCapabilities(signal?: AbortSignal): Promise<CapabilitiesResponse> {
  const response = await fetch(`${API_BASE_URL}/api/capabilities`, { signal });
  if (!response.ok) {
    throw new Error(`获取能力列表失败：HTTP ${response.status}`);
  }
  return (await response.json()) as CapabilitiesResponse;
}

export async function streamQuery(query: string, options: QueryOptions) {
  const threadId = getThreadId();

  // model/capability 按需携带 —— 未选择时字段从请求体消失（"未选 = 自动路由"的协议表达）
  const payload: Record<string, unknown> = { query, thread_id: threadId };
  if (options.model) payload.model = options.model;
  if (options.capability) payload.capability = options.capability;

  const response = await fetch(`${API_BASE_URL}/api/query`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify(payload),
    signal: options.signal,
  });

  if (!response.ok) {
    throw new Error(`接口请求失败：HTTP ${response.status}`);
  }

  if (!response.body) {
    throw new Error("浏览器未返回可读取的流式响应。");
  }

  const reader = response.body.getReader();
  // [修复2] try/finally 保证异常路径（onEvent 抛错/网络中断/abort）也释放 reader 锁；
  // 正常 done 后 releaseLock 是无害 no-op，异常路径上是必需的
  try {
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      // [修复1] CRLF 兼容：帧分隔用 \r?\n\r?\n（SSE 规范允许 CRLF 行尾，
      // 代理/网关也可能改写行尾；固定 \n\n 在中间层改写后会导致整流黏连解析失败）
      const chunks = buffer.split(/\r?\n\r?\n/);
      buffer = chunks.pop() ?? "";

      for (const chunk of chunks) {
        const event = parseSseChunk(chunk);
        if (event) {
          options.onEvent(event);
        }
      }
    }

    // 处理剩余未闭合的数据块
    buffer += decoder.decode();
    const tail = parseSseChunk(buffer);
    if (tail) {
      options.onEvent(tail);
    }
  } finally {
    reader.releaseLock();
  }
}

function parseSseChunk(chunk: string): AgentEvent | null {
  const payload = chunk
    .split(/\r?\n/)                                       // [修复1] 行分隔 CRLF 兼容
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.replace(/^data:\s?/, "").replace(/\r$/, ""))
    .join("\n")
    .trim();

  if (!payload) return null;

  try {
    const parsed: unknown = JSON.parse(payload);
    // [修复4] 最小运行时校验：必须是对象且 type 为字符串。
    // 刻意【不】校验具体事件类型集合 —— 00 §4.4 允许后端新增事件类型，
    // 前端必须容忍未知 type；这里只挡"根本不是事件"的载荷
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      typeof (parsed as { type?: unknown }).type !== "string"
    ) {
      return {
        type: "error",
        message: `后端事件缺少 type 字段：${payload.slice(0, 100)}`,
      };
    }
    return parsed as AgentEvent;
  } catch {
    // [00 §4.4] 解析失败不得中断连接，降级为本地 error 事件
    return {
      type: "error",
      message: `无法解析后端事件：${payload.slice(0, 100)}`,
    };
  }
}
