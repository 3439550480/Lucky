/**
 * 聊天消息气泡组件（Lucky）
 * 组合展示用户问题、智能体回复、执行流程和结果表格；
 * 解释文本走 Markdown 渲染（GFM + 代码高亮，双主题适配）
 */
import { Bot, Copy, UserRound } from "lucide-react";
import { ConfirmCard } from "./ConfirmCard";
import { Markdown } from "./Markdown";
import { ResultTable } from "./ResultTable";
import { StepRail } from "./StepRail";
import { cn, formatTime, toClipboardText } from "../lib/format";
import type { ChatMessage } from "../types/agent";

export function MessageBubble({
  message,
  onFlowConfirm,
}: {
  message: ChatMessage;
  // [S3 结构化确认] 按钮点击 → 提交结构化决定（缺省则不渲染按钮，如只读预览场景）
  onFlowConfirm?: (message: ChatMessage, decision: "confirm" | "cancel") => void;
}) {
  const isUser = message.role === "user";

  const copy = async () => {
    const text = message.result ? toClipboardText(message.result) : message.content;
    await navigator.clipboard.writeText(text);
  };

  return (
    <article className={cn("group flex gap-3", isUser && "justify-end")}>
      {!isUser && (
        <div className="mt-1 grid h-9 w-9 shrink-0 place-items-center rounded-full bg-gradient-to-br from-accent to-accent/70 text-white">
          <Bot className="h-4 w-4" aria-hidden="true" />
        </div>
      )}

      <div className={cn("max-w-[920px] flex-1", isUser && "flex max-w-[760px] justify-end")}>
        <div
          className={cn(
            "relative rounded-xl2 border px-5 py-4 shadow-soft",
            isUser
              ? "border-transparent bg-accent text-white"
              : "border-line bg-surface text-content",
          )}
        >
          <div className="flex items-start justify-between gap-3">
            <p className="whitespace-pre-wrap text-[15px] leading-7">{message.content}</p>
            {!isUser && message.status !== "streaming" && (
              <button
                type="button"
                onClick={copy}
                className="shrink-0 rounded-full p-1.5 text-muted opacity-0 outline-none transition hover:bg-surface-2 hover:text-content focus:opacity-100 focus:ring-2 focus:ring-accent/40 group-hover:opacity-100"
                title="复制"
                aria-label="复制"
              >
                <Copy className="h-4 w-4" aria-hidden="true" />
              </button>
            )}
          </div>

          {message.error && (
            <div className="mt-3 rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
              {message.error}
            </div>
          )}

          {!isUser && <StepRail steps={message.steps} />}
          {!isUser && message.explanation && (
            <div className="mt-3 rounded-lg border-l-4 border-accent bg-surface-2 px-4 py-3 text-sm leading-6">
              <Markdown text={message.explanation} />
            </div>
          )}
          {!isUser && message.result !== undefined && <ResultTable data={message.result} />}
          {!isUser && message.confirm && (
            <ConfirmCard
              request={message.confirm}
              disabled={message.confirm.decided !== undefined}
              onDecide={(decision) => onFlowConfirm?.(message, decision)}
            />
          )}
          {!isUser && message.replenish && (
            <div className="mt-3">
              <p className="mb-1 text-xs font-semibold text-muted">补货建议（按断货紧急度排序）</p>
              <ResultTable data={message.replenish} />
            </div>
          )}

          <div
            className={cn(
              "mt-3 text-xs",
              isUser ? "text-white/60" : "text-muted",
            )}
          >
            {formatTime(message.createdAt)}
          </div>
        </div>
      </div>

      {isUser && (
        <div className="mt-1 grid h-9 w-9 shrink-0 place-items-center rounded-full bg-surface-2 text-content">
          <UserRound className="h-4 w-4" aria-hidden="true" />
        </div>
      )}
    </article>
  );
}
