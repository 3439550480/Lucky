/**
 * 聊天输入区组件（02 文档 §3.5/§3.7）
 * 职责：问题输入、发送/停止、模型选择（上拉弹层）、能力芯片（tier-0 模式开关）
 * 数据来源：models/capabilities 全部由 App.tsx 从后端拉取后经 props 传入 —— 组件不硬编码
 */
import { ArrowUp, Check, ChevronUp, Search, Square, WandSparkles } from "lucide-react";
import {
  FormEvent,
  KeyboardEvent as ReactKeyboardEvent,
  useEffect,
  useRef,
  useState,
} from "react";
import { cn } from "../lib/format";
import type { CapabilityInfo, ModelInfo } from "../types/agent";

type ComposerProps = {
  value: string;
  disabled: boolean;
  isStreaming: boolean;
  models: ModelInfo[];              // 空数组 = 后端不可达，模型按钮隐藏
  defaultModel: string;             // 后端 default；无模型数据时为 ""
  selectedModel: string;            // 受控值（localStorage 持久化在 App 层）
  onModelChange: (name: string) => void;
  capabilities: CapabilityInfo[];   // 空数组 = 芯片不渲染
  selectedCapability: string;       // 空串 = 未选芯片（自动路由）
  onCapabilityChange: (name: string) => void;   // 传空串 = 取消选中
  onChange: (value: string) => void;
  onSubmit: () => void;
  onStop: () => void;
};

// DeepSeek 峰谷是计费时段（后端按调用时间自动判定），UI 不呈现、不可选 —— 02 文档 §3.5 决策
export function Composer({
  value, disabled, isStreaming,
  models, defaultModel, selectedModel, onModelChange,
  capabilities, selectedCapability, onCapabilityChange,
  onChange, onSubmit, onStop,
}: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const [modelPanelOpen, setModelPanelOpen] = useState(false);
  const modelBoxRef = useRef<HTMLDivElement | null>(null);

  // 弹层关闭三通道：外点（pointerdown + contains）、Escape；监听仅面板打开时挂载
  useEffect(() => {
    if (!modelPanelOpen) return;
    const onPointerDown = (e: PointerEvent) => {
      if (modelBoxRef.current && !modelBoxRef.current.contains(e.target as Node)) {
        setModelPanelOpen(false);
      }
    };
    const onEscape = (e: KeyboardEvent) => {
      if (e.key === "Escape") setModelPanelOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onEscape);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onEscape);
    };
  }, [modelPanelOpen]);

  // 分组渲染数据：按 group 聚合（保持后端返回顺序）
  const groups = models.reduce<Record<string, ModelInfo[]>>((acc, m) => {
    (acc[m.group] ??= []).push(m);
    return acc;
  }, {});
  const selectedInfo = models.find((m) => m.name === selectedModel);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!disabled) onSubmit();
  };

  const onKeyDown = (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (!disabled) onSubmit();
    }
  };

  return (
    <div className="border-t border-ink/10 bg-parchment/80 px-4 py-4 backdrop-blur">
      {/* 能力芯片行（输入框上方，用户指定位置）：selectable=true 的能力，持续选中模式 */}
      {capabilities.length > 0 && (
        <div className="mx-auto mb-2 flex max-w-5xl gap-2">
          {capabilities.map((cap) => {
            const active = selectedCapability === cap.name;
            return (
              <button
                key={cap.name}
                type="button"
                title={cap.description}
                disabled={isStreaming}
                onClick={() => onCapabilityChange(active ? "" : cap.name)}
                className={cn(
                  "flex items-center gap-1.5 rounded-md border px-3 py-1 text-sm transition",
                  active
                    ? "border-moss bg-moss/15 text-moss"
                    : "border-ink/15 bg-white/60 text-ink/60 hover:border-ink/30",
                )}
              >
                <Search className="h-3.5 w-3.5" aria-hidden="true" />
                {cap.name === "dataquery" ? "数据查询" : cap.name}
              </button>
            );
          })}
        </div>
      )}

      <form onSubmit={submit}>
        <div className="relative mx-auto flex max-w-5xl items-end gap-3 border border-ink/15 bg-white/75 p-2 shadow-panel">
          {/* 模型选择按钮 + 上拉弹层（数据源 /api/models，前端零硬编码） */}
          {models.length > 0 && (
            <div ref={modelBoxRef} className="relative shrink-0">
              <button
                type="button"
                aria-haspopup="listbox"
                aria-expanded={modelPanelOpen}
                disabled={isStreaming}
                onClick={() => setModelPanelOpen((open) => !open)}
                className="flex h-11 items-center gap-1 bg-ink/5 px-2 text-sm text-ink/70 hover:bg-ink/10"
                title={selectedInfo ? `模型：${selectedInfo.model}` : "选择模型"}
              >
                <WandSparkles className="h-4 w-4" aria-hidden="true" />
                <span className="hidden max-w-28 truncate sm:inline">
                  {selectedInfo?.model ?? "选择模型"}
                </span>
                <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" />
              </button>

              {modelPanelOpen && (
                <div
                  role="listbox"
                  className="absolute bottom-full left-0 z-20 mb-2 max-h-72 w-64 overflow-y-auto border border-ink/15 bg-white shadow-panel"
                >
                  {Object.entries(groups).map(([group, items]) => (
                    <div key={group}>
                      <div className="bg-ink/5 px-3 py-1 text-xs text-ink/50">{group}</div>
                      {items.map((m) => {
                        const active = m.name === selectedModel;
                        return (
                          <button
                            key={m.name}
                            type="button"
                            role="option"
                            aria-selected={active}
                            title={m.model}
                            onClick={() => { onModelChange(m.name); setModelPanelOpen(false); }}
                            className={cn(
                              "flex w-full items-center justify-between px-3 py-2 text-left text-sm hover:bg-ink/5",
                              active && "text-moss",
                            )}
                          >
                            <span>{m.model}</span>
                            {active && <Check className="h-4 w-4" aria-hidden="true" />}
                          </button>
                        );
                      })}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          <textarea
            ref={textareaRef}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={onKeyDown}
            rows={1}
            placeholder="问一个电商数据问题..."
            className="max-h-36 min-h-11 flex-1 resize-none bg-transparent px-2 py-3 text-[15px] leading-6 text-ink outline-none placeholder:text-ink/35"
          />
          <button
            type={isStreaming ? "button" : "submit"}
            onClick={isStreaming ? onStop : undefined}
            disabled={!isStreaming && disabled}
            className={cn(
              "grid h-11 w-11 shrink-0 place-items-center rounded-full text-white transition focus:outline-none focus:ring-2 focus:ring-moss/40 focus:ring-offset-2",
              isStreaming
                ? "bg-tomato hover:bg-tomato/90"
                : "bg-ink hover:bg-soot disabled:cursor-not-allowed disabled:bg-ink/25",
            )}
            title={isStreaming ? "停止" : "发送"}
            aria-label={isStreaming ? "停止" : "发送"}
          >
            {isStreaming ? (
              <Square className="h-4 w-4" aria-hidden="true" />
            ) : (
              <ArrowUp className="h-4 w-4" aria-hidden="true" />
            )}
          </button>
        </div>
      </form>
    </div>
  );
}
