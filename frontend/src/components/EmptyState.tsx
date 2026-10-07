/**
 * 首页空状态（Lucky 能力卡片化）
 * Lucky 欢迎语 + 能力卡片（问数实卡 + 预留位）+ 可点击示例问句（问数/闲聊/帮助多样化）
 */
import { BarChart3, MessagesSquare, ShieldQuestion, Sparkles, Wrench } from "lucide-react";

type EmptyStateProps = {
  examples: string[];
  onUseExample: (example: string) => void;
};

// 能力卡片：第一张是已上线的问数能力，其余为预留位（后端 capability_config 注册后点亮）
const capabilities = [
  {
    icon: BarChart3,
    name: "数据查询",
    desc: "用自然语言查询数仓：销售额、订单、会员、商品……自动生成 SQL 并返回表格",
    available: true,
  },
  {
    icon: MessagesSquare,
    name: "自由对话",
    desc: "任何问题直接问，Lucky 会自己判断是闲聊还是需要查数据",
    available: true,
  },
  {
    icon: Wrench,
    name: "更多技能",
    desc: "图表生成、报告导出、定时任务……敬请期待",
    available: false,
  },
  {
    icon: ShieldQuestion,
    name: "敬请期待",
    desc: "新能力持续接入中",
    available: false,
  },
];

export function EmptyState({ examples, onUseExample }: EmptyStateProps) {
  return (
    <div className="mx-auto flex min-h-full max-w-5xl flex-col justify-center px-4 py-12">
      <div className="mb-10 max-w-3xl">
        <div className="mb-5 inline-flex items-center gap-2 rounded-full border border-accent/25 bg-accent/10 px-3 py-1.5 text-sm font-semibold text-accent">
          <Sparkles className="h-4 w-4" aria-hidden="true" />
          Lucky
        </div>
        <h1 className="text-balance text-4xl font-semibold leading-tight sm:text-5xl">
          你好，我是 <span className="text-accent">Lucky</span>
        </h1>
        <p className="mt-3 text-base leading-7 text-muted">
          你的智能助手：会查数据，也能闲聊。直接输入问题，或从下面的示例开始。
        </p>
      </div>

      {/* 能力卡片 */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {capabilities.map((cap) => {
          const Icon = cap.icon;
          return (
            <div
              key={cap.name}
              className={
                cap.available
                  ? "rounded-xl2 border border-line bg-surface p-4 shadow-soft transition hover:-translate-y-0.5 hover:border-accent/40"
                  : "rounded-xl2 border border-dashed border-line bg-surface/50 p-4 opacity-60"
              }
            >
              <div
                className={
                  cap.available
                    ? "mb-4 grid h-9 w-9 place-items-center rounded-lg bg-accent/12 text-accent"
                    : "mb-4 grid h-9 w-9 place-items-center rounded-lg bg-surface-2 text-muted"
                }
              >
                <Icon className="h-5 w-5" aria-hidden="true" />
              </div>
              <div className="text-sm font-semibold">{cap.name}</div>
              <div className="mt-1.5 text-xs leading-5 text-muted">{cap.desc}</div>
              {!cap.available && (
                <div className="mt-2 inline-block rounded-full bg-surface-2 px-2 py-0.5 text-[10px] text-muted">
                  预留
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* 示例问句 */}
      <div className="mt-8">
        <div className="mb-3 text-xs font-semibold uppercase tracking-[0.16em] text-muted">
          试试这样问
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          {examples.map((example) => (
            <button
              key={example}
              type="button"
              onClick={() => onUseExample(example)}
              className="min-h-14 rounded-xl2 border border-line bg-surface px-4 py-3.5 text-left text-[15px] leading-6 transition hover:-translate-y-0.5 hover:border-accent/40 hover:shadow-soft focus:outline-none focus:ring-2 focus:ring-accent/35"
            >
              {example}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
