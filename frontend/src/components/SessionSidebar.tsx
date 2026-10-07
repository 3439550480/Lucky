/**
 * 会话历史侧栏（Lucky）
 * 品牌区 + 新会话 + 会话列表（切换/删除）+ 底部状态
 */
import { BarChart3, MessageSquarePlus, Server, Trash2 } from "lucide-react";
import { cn } from "../lib/format";
import type { SessionMeta } from "../lib/sessionStore";

interface SessionSidebarProps {
  sessions: SessionMeta[];
  activeId: string;
  isStreaming: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
  apiBaseUrl: string;
}

function relativeTime(ts: number): string {
  const diff = Date.now() - ts;
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  return `${Math.floor(diff / 86_400_000)} 天前`;
}

export default function SessionSidebar({
  sessions,
  activeId,
  isStreaming,
  onSelect,
  onNew,
  onDelete,
  apiBaseUrl,
}: SessionSidebarProps) {
  return (
    <aside className="hidden min-h-0 flex-col border-r border-line bg-surface/85 backdrop-blur lg:flex">
      <div className="border-b border-line px-5 py-5">
        <div className="flex items-center gap-3">
          <div className="grid h-10 w-10 place-items-center rounded-xl2 bg-gradient-to-br from-accent to-accent/70 text-white shadow-soft">
            <BarChart3 className="h-5 w-5" aria-hidden="true" />
          </div>
          <div>
            <div className="text-base font-semibold tracking-wide">Lucky</div>
            <div className="text-xs text-muted">智能助手</div>
          </div>
        </div>
      </div>

      <div className="px-4 pt-4">
        <button
          type="button"
          onClick={onNew}
          disabled={isStreaming}
          className="flex h-10 w-full items-center justify-center gap-2 rounded-lg bg-accent text-sm font-semibold text-white transition hover:bg-accent/85 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <MessageSquarePlus className="h-4 w-4" aria-hidden="true" />
          新会话
        </button>
      </div>

      <nav className="min-h-0 flex-1 space-y-1 overflow-y-auto px-3 py-3" aria-label="会话历史">
        {sessions.map((session) => (
          <div
            key={session.id}
            className={cn(
              "group relative flex items-center rounded-lg px-3 py-2.5 text-sm transition-colors",
              session.id === activeId
                ? "bg-accent/10 text-content"
                : "text-muted hover:bg-surface-2 hover:text-content",
            )}
          >
            <button
              type="button"
              onClick={() => onSelect(session.id)}
              className="min-w-0 flex-1 text-left"
              title={session.title}
            >
              <div className="truncate font-medium">{session.title}</div>
              <div className="mt-0.5 text-xs text-muted">{relativeTime(session.updatedAt)}</div>
            </button>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onDelete(session.id);
              }}
              disabled={isStreaming}
              className="ml-1 hidden h-7 w-7 shrink-0 place-items-center rounded-md text-muted transition hover:bg-danger/10 hover:text-danger group-hover:grid disabled:opacity-40"
              title="删除会话"
              aria-label={`删除会话 ${session.title}`}
            >
              <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
            {session.id === activeId && (
              <span className="absolute left-0 top-1/2 h-5 w-0.5 -translate-y-1/2 rounded-full bg-accent" />
            )}
          </div>
        ))}
      </nav>

      <div className="border-t border-line p-4">
        <div className="flex items-center justify-between gap-3 text-xs text-muted">
          <span className="inline-flex items-center gap-2">
            <Server className="h-3.5 w-3.5" aria-hidden="true" />
            API
          </span>
          <span className="truncate font-mono">{apiBaseUrl}</span>
        </div>
      </div>
    </aside>
  );
}
