/**
 * 前端应用主组件
 * 负责聊天会话状态、SSE 事件消费和整体页面布局
 */
import {
  Activity,
  BarChart3,
  Eraser,
  History,
  Leaf,
  MessageSquarePlus,
  Server,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Composer } from "./components/Composer";
import { EmptyState } from "./components/EmptyState";
import { MessageBubble } from "./components/MessageBubble";
import { streamQuery } from "./lib/agentApi";
import { fetchCapabilities, fetchModels, isAbortError } from "./lib/agentApi";
import { cn, summarizeResult } from "./lib/format";
import type { AgentEvent, CapabilityInfo, ChatMessage, ModelInfo, StepState } from "./types/agent";
import ManualPage from "./components/ManualPage";

const examples = [
  "统计 2025 年第一季度各大区的 GMV，并按 GMV 从高到低排序",
  "统计 2025 年 3 月各商品品类的销量和销售额",
  "查询华东地区 2025 年第一季度销售额最高的前 5 个商品",
  "按会员等级统计 2025 年第一季度的订单数和销售额",
];

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "Vite /api proxy";

function makeId() {
  return crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
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
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [activeController, setActiveController] = useState<AbortController | null>(null);
  const [showManual, setShowManual] = useState(false);
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

  // 首次访问自动显示手册
  useEffect(() => {
    const hasVisited = localStorage.getItem("hasVisitedManual");
    if (!hasVisited) {
      setShowManual(true);
      localStorage.setItem("hasVisitedManual", "true");
    }
  }, []);

  const isStreaming = Boolean(activeController);
  const canSubmit = draft.trim().length > 0 && !isStreaming;

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
      content: "正在连接问数智能体...",
      createdAt: Date.now(),
      status: "streaming",
      steps: [],
    };

    const controller = new AbortController();
    setActiveController(controller);
    setDraft("");
    setMessages((current) => [...current, userMessage, assistantMessage]);

    const onEvent = (event: AgentEvent) => {
      setMessages((current) =>
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
            case "explanation":   // 新增
              return {
                ...message,
                status: "done",
                explanation: event.text,     // 存储解释文本
                // content: event.text,
                // 可选：如果希望把解释也作为主要内容显示，可以更新 content
                // content: event.text,
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
      setMessages((current) =>
        current.map((message) =>
          message.id === assistantId
            ? {
                ...message,
                status: isAbort ? "done" : "error",
                content: isAbort ? "已停止本次查询。" : "无法连接问数接口。",
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

  const clearConversation = () => {
    if (isStreaming) return;
    setMessages([]);
    setDraft("");
  };

  // 如果显示手册页面，直接渲染手册组件（全屏，无聊天界面）
  if (showManual) {
    return <ManualPage onClose={() => setShowManual(false)} />;
  }

  // 正常聊天界面
  return (
    <div className="h-dvh overflow-hidden bg-parchment text-ink">
      <div className="pointer-events-none fixed inset-0 bg-[linear-gradient(90deg,rgba(32,32,29,0.045)_1px,transparent_1px),linear-gradient(rgba(32,32,29,0.035)_1px,transparent_1px)] bg-[size:48px_48px]" />
      <div className="pointer-events-none fixed inset-0 grain" />

      <div className="relative grid h-full min-h-0 overflow-hidden lg:grid-cols-[300px_minmax(0,1fr)]">
        <aside className="hidden min-h-0 border-r border-ink/10 bg-[#efe6d8]/85 backdrop-blur lg:flex lg:flex-col">
          <div className="border-b border-ink/10 px-5 py-5">
            <div className="flex items-center gap-3">
              <div className="grid h-10 w-10 place-items-center bg-ink text-parchment">
                <BarChart3 className="h-5 w-5" aria-hidden="true" />
              </div>
              <div>
                <div className="text-base font-semibold tracking-[0.02em]">电商问数</div>
                <div className="text-xs text-ink/50">shopkeeper-agent</div>
              </div>
            </div>
          </div>

          <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-4 py-4">
            <button
              type="button"
              onClick={clearConversation}
              disabled={isStreaming}
              className="flex h-11 w-full items-center justify-center gap-2 bg-ink text-sm font-semibold text-parchment transition hover:bg-soot disabled:cursor-not-allowed disabled:bg-ink/35"
            >
              <MessageSquarePlus className="h-4 w-4" aria-hidden="true" />
              新会话
            </button>

            <section>
              <div className="mb-2 flex items-center gap-2 px-1 text-xs font-semibold uppercase tracking-[0.16em] text-ink/45">
                <History className="h-3.5 w-3.5" aria-hidden="true" />
                样例
              </div>
              <div className="space-y-2">
                {examples.map((example) => (
                  <button
                    key={example}
                    type="button"
                    disabled={isStreaming}
                    onClick={() => startQuery(example)}
                    className="w-full border border-ink/10 bg-white/42 px-3 py-3 text-left text-sm leading-5 text-ink/75 transition hover:border-moss/35 hover:bg-white/75 disabled:cursor-not-allowed disabled:opacity-55"
                  >
                    {example}
                  </button>
                ))}
              </div>
            </section>
          </div>

          <div className="border-t border-ink/10 p-4">
            <div className="grid gap-2 text-xs text-ink/55">
              <div className="flex items-center justify-between gap-3">
                <span className="inline-flex items-center gap-2">
                  <Server className="h-3.5 w-3.5" aria-hidden="true" />
                  API
                </span>
                <span className="truncate font-mono">{API_BASE_URL}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="inline-flex items-center gap-2">
                  <Activity className="h-3.5 w-3.5" aria-hidden="true" />
                  完成
                </span>
                <span>{completedCount}</span>
              </div>
            </div>
          </div>
        </aside>

        <main className="flex min-h-0 min-w-0 flex-col overflow-hidden">
          <header className="flex h-16 shrink-0 items-center justify-between border-b border-ink/10 bg-parchment/88 px-4 backdrop-blur lg:px-6">
            <div className="flex min-w-0 items-center gap-3">
              <div className="grid h-9 w-9 shrink-0 place-items-center bg-moss text-white lg:hidden">
                <BarChart3 className="h-4 w-4" aria-hidden="true" />
              </div>
              <div className="min-w-0">
                <div className="truncate text-sm font-semibold text-ink">智能数据分析 Agent</div>
                <div className="truncate text-xs text-ink/45">FastAPI SSE / LangGraph</div>
              </div>
            </div>
            <div className="flex items-center gap-2">
              {/* 操作手册按钮（橡皮擦左边） */}
              <button
                onClick={() => setShowManual(true)}
                className="grid h-9 w-9 place-items-center rounded-full text-ink/55 transition hover:bg-ink/5 hover:text-ink"
                title="操作手册"
                aria-label="操作手册"
              >
                📘
              </button>
              {/* 清空按钮（橡皮擦） */}
              <button
                type="button"
                onClick={clearConversation}
                disabled={messages.length === 0 || isStreaming}
                className={cn(
                  "grid h-9 w-9 place-items-center rounded-full text-ink/55 transition hover:bg-ink/5 hover:text-ink disabled:cursor-not-allowed disabled:opacity-35",
                )}
                title="清空"
                aria-label="清空"
              >
                <Eraser className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
          </header>

          <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
            {messages.length === 0 ? (
              <EmptyState examples={examples} onUseExample={(example) => setDraft(example)} />
            ) : (
              <div className="mx-auto flex max-w-6xl flex-col gap-6 px-4 py-6 lg:px-8">
                {messages.map((message) => (
                  <MessageBubble key={message.id} message={message} />
                ))}
              </div>
            )}
          </div>

          <div className="border-t border-ink/10 bg-[#efe6d8]/45 px-4 py-2 text-center text-xs text-ink/45">
            <span className="inline-flex items-center gap-2">
              <Leaf className="h-3.5 w-3.5 text-moss" aria-hidden="true" />
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