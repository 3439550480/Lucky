/**
 * Lucky 前端应用主组件
 * 聊天会话状态（多会话）· SSE 事件消费 · 整体布局
 * 视觉：语义 token（tailwind 语义色）+ 亮/暗双主题
 */
import { BarChart3, Eraser, Leaf } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Composer } from "./components/Composer";
import { EmptyState } from "./components/EmptyState";
import { MessageBubble } from "./components/MessageBubble";
import SessionSidebar from "./components/SessionSidebar";
import ThemeToggle from "./components/ThemeToggle";
import { streamQuery } from "./lib/agentApi";
import { fetchCapabilities, fetchModels, isAbortError } from "./lib/agentApi";
import { cn, summarizeResult, uuid } from "./lib/format";
import { useSessions } from "./lib/useSessions";
import { useTheme } from "./lib/theme";
import type { AgentEvent, CapabilityInfo, ChatMessage, ModelInfo, StepState } from "./types/agent";

// 示例问句（安踏特卖店：问数 / 库存 / 补货 / 闲聊——对应能力路由的分发面）
const examples = [
  "国庆 7 天哪双鞋卖得最好？卖了多少金额？",
  "现在还有多少库存？哪些 SKU 断码了？",
  "生成鞋类的补货计划",
  "你好，你能做什么？",
];

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "Vite /api proxy";

function makeId() {
  return uuid();
}

function upsertStep(steps: StepState[] = [], event: Extract<AgentEvent, { type: "progress" }>) {
  const next = steps.filter((item) => item.step !== event.step);
  next.push({
    step: event.step,
    status: event.status,
    updatedAt: Date.now(),
  });
  return next;
}

export default function App() {
  // ==== 多会话（Lucky：会话列表/切换/消息持久化，thread_id 与后端 checkpoint 一一对应）====
  const { sessions, activeId, messages, updateMessages, touchSession, newSession, switchTo, deleteSession } =
    useSessions();
  const { theme, toggle: toggleTheme } = useTheme();
  const [draft, setDraft] = useState("");
  const [activeController, setActiveController] = useState<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // ==== 模型与能力芯片（02 文档 §3.6：模型=localStorage 跨会话偏好；thread_id=sessionStorage 会话隔离）====
  const [models, setModels] = useState<ModelInfo[]>([]);            // 空 = 后端不可达，按钮隐藏
  const [defaultModel, setDefaultModel] = useState("");
  const [selectedModel, setSelectedModel] = useState("");           // 空串 = 请求不带 model 字段
  const [capabilities, setCapabilities] = useState<CapabilityInfo[]>([]);
  const [selectedCapability, setSelectedCapability] = useState(""); // 空串 = 未选芯片（自动路由）

  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      try {
        const data = await fetchModels(controller.signal);
        setModels(data.providers);
        setDefaultModel(data.default);
        // localStorage 旧值校验：不再存在的 provider 重置为后端 default
        const saved = localStorage.getItem("agent_model");
        setSelectedModel(saved && data.providers.some((m) => m.name === saved) ? saved : data.default);
      } catch {
        setModels([]);          // 降级：按钮隐藏、请求不带 model（02 §3.5 降级链）
      }
      try {
        const caps = await fetchCapabilities(controller.signal);
        setCapabilities(caps.capabilities);
        const savedCap = localStorage.getItem("agent_capability") ?? "";
        // 持久化的芯片选中值必须仍在可选列表中（否则重置为未选）
        setSelectedCapability(caps.capabilities.some((c) => c.name === savedCap) ? savedCap : "");
      } catch {
        setCapabilities([]);    // 降级：芯片不渲染、走自动路由
      }
    })();
    return () => controller.abort();
  }, []);

  const handleModelChange = (name: string) => {
    setSelectedModel(name);
    localStorage.setItem("agent_model", name);
  };

  // 芯片是"模式开关"：再点取消（传空串）即恢复自动路由（04 文档 tier-0 语义）
  const handleCapabilityChange = (name: string) => {
    setSelectedCapability(name);
    if (name) localStorage.setItem("agent_capability", name);
    else localStorage.removeItem("agent_capability");
  };

  const isStreaming = Boolean(activeController);
  const canSubmit = draft.trim().length > 0 && !isStreaming;
  const activeSession = sessions.find((s) => s.id === activeId);

  const completedCount = useMemo(
    () => messages.filter((message) => message.role === "assistant" && message.status === "done").length,
    [messages],
  );

  useEffect(() => {
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages]);

  const startQuery = async (rawQuery = draft) => {
    const query = rawQuery.trim();
    if (!query || isStreaming) return;

    const userMessage: ChatMessage = {
      id: makeId(),
      role: "user",
      content: query,
      createdAt: Date.now(),
    };

    const assistantId = makeId();
    const assistantMessage: ChatMessage = {
      id: assistantId,
      role: "assistant",
      content: "正在思考...",
      createdAt: Date.now(),
      status: "streaming",
      steps: [],
    };

    const controller = new AbortController();
    setActiveController(controller);
    setDraft("");
    touchSession(query);   // 触活会话（首条用户消息生成标题）
    updateMessages((current) => [...current, userMessage, assistantMessage]);

    const onEvent = (event: AgentEvent) => {
      updateMessages((current) =>
        current.map((message) => {
          if (message.id !== assistantId) return message;

          switch (event.type) {
            case "progress":
              return {
                ...message,
                content: event.status === "running" ? `正在执行：${event.step}` : message.content,
                steps: upsertStep(message.steps, event),
              };
            case "result":
              return {
                ...message,
                status: "done",
                content: summarizeResult(event.data),
                result: event.data,
              };
            case "explanation":
              return {
                ...message,
                status: "done",
                explanation: event.text,
              };
            case "replenish":
              // [2.0 S3b] 补货计划：结构化明细入 message，status 等 explanation 事件置 done
              return {
                ...message,
                replenish: event.plan,
                content: event.total_gap > 0
                  ? `已生成补货计划：${event.scope.length > 0 ? event.scope.join("、") + " " : ""}共 ${event.total_gap} 个缺口 SKU，合计建议补货 ${event.total_suggest_qty} 件`
                  : "当前库存满足覆盖目标，暂无补货缺口。",
              };
            default:
              return {
                ...message,
                status: "error",
                content: "这次查询没有成功。",
                error: (event as any).message,
              };
          }
        }),
      );
    };

    try {
      await streamQuery(query, {
        signal: controller.signal,
        onEvent,
        model: selectedModel || undefined,        // 未选择/降级 → 字段缺省
        capability: selectedCapability || undefined,
      });
    } catch (error) {
      const isAbort = isAbortError(error);
      updateMessages((current) =>
        current.map((message) =>
          message.id === assistantId
            ? {
                ...message,
                status: isAbort ? "done" : "error",
                content: isAbort ? "已停止本次回答。" : "无法连接服务，请稍后重试。",
                error: isAbort ? undefined : error instanceof Error ? error.message : String(error),
              }
            : message,
        ),
      );
    } finally {
      setActiveController(null);
    }
  };

  const stopQuery = () => {
    activeController?.abort();
  };

  const handleNewSession = () => {
    if (isStreaming) return;
    newSession();
    setDraft("");
  };

  const handleDeleteSession = (id: string) => {
    if (isStreaming) return;
    deleteSession(id);
  };

  // 正常聊天界面
  return (
    <div className="h-dvh overflow-hidden bg-bg text-content">
      <div className="pointer-events-none fixed inset-0 grain" />

      <div className="relative grid h-full min-h-0 overflow-hidden lg:grid-cols-[280px_minmax(0,1fr)]">
        <SessionSidebar
          sessions={sessions}
          activeId={activeId}
          isStreaming={isStreaming}
          onSelect={switchTo}
          onNew={handleNewSession}
          onDelete={handleDeleteSession}
          apiBaseUrl={API_BASE_URL}
        />

        <main className="flex min-h-0 min-w-0 flex-col overflow-hidden">
          <header className="flex h-16 shrink-0 items-center justify-between border-b border-line bg-surface/70 px-4 backdrop-blur lg:px-6">
            <div className="flex min-w-0 items-center gap-3">
              <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-gradient-to-br from-accent to-accent/70 text-white lg:hidden">
                <BarChart3 className="h-4 w-4" aria-hidden="true" />
              </div>
              <div className="min-w-0">
                <div className="truncate text-sm font-semibold">{activeSession?.title ?? "新对话"}</div>
                <div className="truncate text-xs text-muted">Lucky · 智能助手</div>
              </div>
            </div>
            <div className="flex items-center gap-1">
              <ThemeToggle theme={theme} onToggle={toggleTheme} />
              <button
                type="button"
                onClick={handleNewSession}
                disabled={messages.length === 0 || isStreaming}
                className="grid h-9 w-9 place-items-center rounded-full text-muted transition hover:bg-surface-2 hover:text-content disabled:cursor-not-allowed disabled:opacity-35 lg:hidden"
                title="新会话"
                aria-label="新会话"
              >
                <Eraser className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
          </header>

          <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
            {messages.length === 0 ? (
              <EmptyState examples={examples} onUseExample={(example) => setDraft(example)} />
            ) : (
              <div className="mx-auto flex max-w-5xl flex-col gap-6 px-4 py-6 lg:px-8">
                {messages.map((message) => (
                  <MessageBubble key={message.id} message={message} />
                ))}
              </div>
            )}
          </div>

          <div className="border-t border-line bg-surface/45 px-4 py-2 text-center text-xs text-muted">
            <span className="inline-flex items-center gap-2">
              <Leaf className="h-3.5 w-3.5 text-accent" aria-hidden="true" />
              {isStreaming ? "运行中" : "就绪"}
            </span>
          </div>
          <Composer
            value={draft}
            disabled={!canSubmit}
            isStreaming={isStreaming}
            models={models}
            defaultModel={defaultModel}
            selectedModel={selectedModel}
            onModelChange={handleModelChange}
            capabilities={capabilities}
            selectedCapability={selectedCapability}
            onCapabilityChange={handleCapabilityChange}
            onChange={setDraft}
            onSubmit={() => startQuery()}
            onStop={stopQuery}
          />
        </main>
      </div>
    </div>
  );
}
