/**
 * 出入库确认卡片（2.0 上下文策略 3.6 · S3 结构化确认）
 * 后端 confirm 事件渲染：把"请回复确认/取消"变成两个按钮。点击后走独立端点，
 * 决定以结构化字段提交——服务端不再猜自由文本（误写库在协议层被消除）。
 */
import { Check, X } from "lucide-react";
import { cn } from "../lib/format";
import type { ConfirmRequest } from "../types/agent";

// 槽位键 → 中文标签（与后端 _SLOT_LABELS 对齐；未列出的键原样显示）
const SLOT_LABELS: Record<string, string> = {
  direction: "方向",
  doc_no: "单据号",
  sku_id: "SKU",
  qty: "数量",
};

const DIRECTION_LABELS: Record<string, string> = { in: "入库", out: "出库" };

function displayValue(key: string, value: string | number): string {
  if (key === "direction") return DIRECTION_LABELS[String(value)] ?? String(value);
  return key === "qty" ? `${value} 件` : String(value);
}

export function ConfirmCard({
  request,
  disabled,
  onDecide,
}: {
  request: ConfirmRequest;
  disabled?: boolean;
  onDecide: (decision: "confirm" | "cancel") => void;
}) {
  const decided = request.decided;

  return (
    <div className="mt-3 rounded-lg border border-line bg-surface-2 px-4 py-3">
      <p className="text-xs font-semibold text-muted">待确认操作（提交后立即记账）</p>

      <dl className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-sm">
        {Object.entries(request.slots).map(([key, value]) => (
          <div key={key} className="flex items-baseline gap-1.5">
            <dt className="text-muted">{SLOT_LABELS[key] ?? key}</dt>
            <dd className="font-medium">{displayValue(key, value)}</dd>
          </div>
        ))}
      </dl>

      {decided ? (
        <p className="mt-3 text-sm text-muted">
          {decided === "confirm" ? "已提交确认，正在记账…" : "已取消本次操作。"}
        </p>
      ) : (
        <div className="mt-3 flex flex-wrap gap-2">
          <button
            type="button"
            disabled={disabled}
            onClick={() => onDecide("confirm")}
            className="inline-flex items-center gap-1.5 rounded-full bg-accent px-4 py-1.5 text-sm font-medium text-white transition hover:bg-accent/90 focus:outline-none focus:ring-2 focus:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Check className="h-4 w-4" aria-hidden="true" />
            确认提交
          </button>
          <button
            type="button"
            disabled={disabled}
            onClick={() => onDecide("cancel")}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-full border border-line px-4 py-1.5 text-sm font-medium",
              "text-content transition hover:bg-surface focus:outline-none focus:ring-2 focus:ring-accent/40",
              "disabled:cursor-not-allowed disabled:opacity-40",
            )}
          >
            <X className="h-4 w-4" aria-hidden="true" />
            取消
          </button>
        </div>
      )}
    </div>
  );
}
