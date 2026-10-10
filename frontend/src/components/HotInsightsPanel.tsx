/**
 * 热卖排行侧边栏面板（2.0）
 * 用户拍板：热卖排行移出对话能力 → 前端固定面板（/api/insights/hot，登录即可见）
 */
import { RotateCw, TrendingUp } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { fetchHotInsights } from "../lib/agentApi";
import type { HotInsight } from "../lib/agentApi";

export default function HotInsightsPanel() {
  const [items, setItems] = useState<HotInsight[]>([]);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setFailed(false);
    try {
      setItems(await fetchHotInsights());
    } catch {
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const maxQty = items[0]?.quantity ?? 1;

  return (
    <div className="border-t border-line px-4 py-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-content">
          <TrendingUp className="h-3.5 w-3.5 text-accent" aria-hidden="true" />
          热卖排行
        </span>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          className="rounded-md p-1 text-muted transition hover:bg-surface-2 hover:text-content disabled:opacity-40"
          title="刷新"
          aria-label="刷新热卖排行"
        >
          <RotateCw className={loading ? "h-3.5 w-3.5 animate-spin" : "h-3.5 w-3.5"} aria-hidden="true" />
        </button>
      </div>

      {failed ? (
        <p className="py-1 text-xs text-muted">暂时获取不到排行，点上方刷新重试。</p>
      ) : items.length === 0 && !loading ? (
        <p className="py-1 text-xs text-muted">暂无销售数据。</p>
      ) : (
        <ul className="space-y-2" aria-label="按销量排序的热卖商品">
          {items.map((item, index) => (
            <li key={item.product_name}>
              <div className="flex items-baseline justify-between gap-2 text-xs">
                <span className="truncate text-muted">
                  <span className="mr-1 font-mono text-[10px] text-muted/60">{index + 1}</span>
                  {item.product_name}
                </span>
                <span className="shrink-0 font-mono text-[10px] text-muted">{item.quantity} 件</span>
              </div>
              <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface-2">
                <div
                  className="h-full rounded-full bg-accent/80 transition-all"
                  style={{ width: `${Math.max(6, (item.quantity / maxQty) * 100)}%` }}
                />
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
