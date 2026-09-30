"""
评测 CLI（03 文档 §2.3 / §3.8）

用法：
uv run python -m app.scripts.run_evaluation -d evaluation/datasets/eval_v1.json -e baseline_5intent
退出码：0=完成（含单用例失败也算完成）；2=数据集加载失败；3=无 enabled 用例
"""
import argparse
import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

from app.core.log import logger
from app.evaluation.dataset import load_dataset
from app.evaluation.report import (_git_commit, compare_reports, compute_aggregate,
                                    render_json, render_markdown)
from app.evaluation.runner import EvaluationRunner, flags_snapshot


def parse_features(pairs: list[str]) -> dict[str, str]:
    """--features key=value 列表 → dict（格式错误直接报错退出）"""
    out: dict[str, str] = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--features 格式错误：{p}（应为 key=value，如 memory.short_term=false）")
        k, _, v = p.partition("=")
        out[k.strip()] = v.strip()
    return out


def _pct(v) -> str:
    """None → '—'；比例 → 百分数"""
    return "—" if v is None else f"{v * 100:.1f}%"


def _print_summary(aggregate: dict) -> None:
    """控制台指标摘要表（与 JSON/Markdown 同源的 aggregate，口径一致）"""
    q, cost = aggregate["quality"], aggregate["cost"]
    print("\n----- 质量指标 -----")
    if not q["retrieval"].get("disabled"):
        for ch, m in q["retrieval"].items():
            print(f"  检索[{ch:8}] hit@k={_pct(m['hit_at_k'])} mrr={_pct(m['mrr'])} "
                  f"p@k={_pct(m['precision_at_k'])} r@k={_pct(m['recall_at_k'])}")
    if not q["intent"].get("disabled"):
        print(f"  意图准确率: {_pct(q['intent'].get('accuracy'))} (support={q['intent'].get('support')})")
    if not q["tool"].get("disabled"):
        print(f"  工具触发: {_pct(q['tool'].get('invocation_accuracy'))} (support={q['tool'].get('support')})")
    if not q["sql"].get("disabled"):
        print(f"  SQL 可执行率: {_pct(q['sql'].get('executability'))}  "
              f"结果正确性: {_pct(q['sql'].get('correctness'))}")
    print("----- 成本 -----")
    if not cost.get("disabled"):
        print(f"  总费用: {cost['total_cost']} 元  每问: {cost['avg_cost_per_case']} 元  "
              f"tokens: {cost['tokens'].get('total')}  峰谷: {cost['tier_distribution']}")


async def main() -> int:
    # step 1: 参数解析（§2.3）
    ap = argparse.ArgumentParser(description="Agent 评估（03 文档）")
    ap.add_argument("-d", "--dataset", required=True, help="评测集 JSON 路径（必填）")
    ap.add_argument("-e", "--exp-label", required=True, help="实验标签（必填，报告文件名与对比维度）")
    ap.add_argument("--provider", default=None, help="LLM provider（缺省 = app_config.llm.default）")
    ap.add_argument("--pricing-tier", default=None, choices=["peak", "offpeak"],
                    help="DeepSeek 计费档位强制固定（缺省 auto 按调用时间判定）")
    ap.add_argument("--features", action="append", default=[], metavar="KEY=VALUE",
                    help="开关覆盖，可多次出现（如 memory.short_term=false）")
    ap.add_argument("--k", type=int, default=None, help="覆盖 defaults.k")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 个 enabled 用例（调试用）")
    ap.add_argument("--interval", type=int, default=2, help="用例间隔秒数（限流保护，缺省 2）")
    ap.add_argument("--compare", nargs="*", default=None,
                    help="对比已有报告 JSON（可多个，与本场报告并排输出对比）")
    args = ap.parse_args()

    # step 2: 加载数据集（失败 → 退出码 2；无 enabled 用例 → 3）
    try:
        dataset = load_dataset(Path(args.dataset))
    except ValueError as e:
        logger.error(str(e))
        return 2
    if args.k:
        for c in dataset.cases:
            c.k = args.k
    if args.limit:
        dataset.cases = dataset.cases[:args.limit]
    # 全 disabled（骨架数据集）→ 照常跑空流程并出报告（验收标准 1：support=0 显示 null，退出码 0）。
    # 注：03 文档 §3.8 的"退出码 3=无 enabled 用例"与验收标准 1 冲突，此处以验收标准 1 为准
    if not dataset.cases:
        logger.warning("[eval] 数据集没有 enabled 用例，按空流程生成 null 指标报告（退出码 0）")

    # step 3: 执行评测
    overrides = parse_features(args.features)
    started = time.time()
    runner = EvaluationRunner(dataset, provider=args.provider,
                              feature_overrides=overrides,
                              interval=args.interval, pricing_tier=args.pricing_tier)
    results = await runner.run()

    # step 4: 报告（meta 记录完整可复现信息，§5.2）
    meta = {
        "exp_label": args.exp_label,
        "provider": runner.provider,
        "pricing_tier": args.pricing_tier or "auto",
        "forced_tier": args.pricing_tier,            # None=auto；peak/offpeak=强制（成本模块消费）
        "dataset_id": dataset.dataset_id,
        "dataset_path": args.dataset,
        "feature_overrides": overrides,
        "flags_snapshot": flags_snapshot(),
        "forced": {"usage_tracking": True},          # §5.2 强制项入报告
        "total_cases": len(results),
        "ok_cases": sum(1 for r in results if r.status == "ok"),
        "graph_error_cases": sum(1 for r in results if r.status == "graph_error"),
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "duration_s": round(time.time() - started, 1),
        "git_commit": _git_commit(),
    }
    aggregate = compute_aggregate(results, meta)
    json_path = render_json(results, dataset, meta, aggregate)
    md_path = render_markdown(results, dataset, meta, aggregate)

    # step 5: 控制台摘要 + 可选对比
    print(f"\n===== 评测完成：{args.exp_label} =====")
    print(f"用例：{meta['ok_cases']} ok / {meta['graph_error_cases']} 失败，耗时 {meta['duration_s']}s")
    _print_summary(aggregate)
    print(f"\n报告：{json_path}\n      {md_path}")
    if args.compare:
        compare_path = compare_reports([json_path, *[Path(p) for p in args.compare]])
        print(f"对比报告：{compare_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
