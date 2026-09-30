"""
报告渲染与实验对比（03 文档 §3.7）

输出：JSON（机器对比用）+ Markdown（人读）双格式，文件名 {exp_label}_{时间戳}；
meta 记录完整可复现信息（provider/dataset/flags 快照/git commit/开关强制项）；
compare_reports 多报告并排对比（行=实验标签，列=关键指标）
"""
import json
import subprocess
from datetime import datetime
from pathlib import Path

from app.conf.app_config import app_config
from app.core.log import logger
from app.evaluation.cost_metrics import aggregate_costs, compute_case_cost
from app.evaluation.intent_metrics import compute_intent_metrics
from app.evaluation.retrieval_metrics import compute_retrieval_metrics
from app.evaluation.runner import CaseResult, flags_snapshot
from app.evaluation.sql_metrics import compute_sql_metrics
from app.evaluation.tool_metrics import compute_tool_metrics, extract_invoked_tools

REPORTS_DIR = Path(__file__).resolve().parents[2] / "evaluation" / "reports"


def _git_commit() -> str | None:
    """当前 commit（非 git 环境/命令失败返回 None，不阻断报告）"""
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


def _macro_avg(values: list[float]) -> float | None:
    """逐用例宏平均（§3.1：每用例等权）；空列表 → None（绝不输出 0 分，§5.1）"""
    return sum(values) / len(values) if values else None


def _fmt(v, pct: bool = False) -> str:
    """Markdown 单元格格式化：None → '—'；比例 → 百分数一位小数"""
    if v is None:
        return "—"
    if pct and isinstance(v, (int, float)):
        return f"{v * 100:.1f}%"
    return str(v)


def compute_aggregate(results: list[CaseResult], meta: dict) -> dict:
    """聚合全部指标（JSON/Markdown/控制台三方共用同一份结果，口径绝不两算）。
    evaluation.* 开关关闭的区输出 {"disabled": true}（§5.1：不计算不呈现）"""
    ev = app_config.features.evaluation
    ok = [r for r in results if r.status == "ok"]
    quality: dict = {}
    # ---- 检索（三通道独立宏平均，§3.1）----
    if ev.retrieval_metrics:
        channels = ("columns", "metrics", "values", "tables")
        per_channel = {ch: {"hit_at_k": [], "mrr": [], "precision_at_k": [], "recall_at_k": []}
                       for ch in channels}
        for r in ok:
            m = compute_retrieval_metrics(r.final_state, r.case.expected, r.case.k, r.case.match_mode)
            for ch in channels:
                if m[ch]:
                    for k in per_channel[ch]:
                        per_channel[ch][k].append(m[ch][k])
        quality["retrieval"] = {ch: {k: _macro_avg(v) for k, v in per_channel[ch].items()}
                                for ch in channels}
    else:
        quality["retrieval"] = {"disabled": True}
    # ---- 意图 ----
    if ev.intent_metrics:
        pairs = [{"expected_intent": r.case.expected["intent"],
                  "actual_intent": r.final_state.get("intent", "")}
                 for r in ok if "intent" in r.case.expected]
        quality["intent"] = compute_intent_metrics(pairs)
    else:
        quality["intent"] = {"disabled": True}
    # ---- 工具触发（排除用户显式选择；runner 恒空芯片，此处双保险，§3.9 口径补充）----
    if ev.tool_metrics:
        pairs = [{"expected_tools": r.case.expected.get("tools"),
                  "invoked_tools": extract_invoked_tools(r.final_state)}
                 for r in ok
                 if r.case.expected.get("tools") is not None
                 and r.final_state.get("capability_source") != "user"]
        quality["tool"] = compute_tool_metrics(pairs)
    else:
        quality["tool"] = {"disabled": True}
    # ---- SQL（可执行率分母=全部用例含失败；正确性分母=golden 可比用例，§3.3）----
    if ev.sql_metrics:
        quality["sql"] = compute_sql_metrics([r.sql_metrics_input for r in results])
    else:
        quality["sql"] = {"disabled": True}
    # ---- 成本 ----
    if ev.cost_metrics:
        provider_cfg = app_config.llm.providers.get(meta["provider"], {})
        costs = [compute_case_cost(r.tracker_records, r.tracker_summary or {},
                                   provider_cfg, meta.get("forced_tier"))
                 for r in results if r.tracker_summary]
        cost = aggregate_costs(costs)
    else:
        cost = {"disabled": True,
                "note": "token 明细仍在 cases 中（tracker 不受开关影响，可回算，§5.1）"}
    return {"quality": quality, "cost": cost}


def _cases_detail(results: list[CaseResult], meta: dict) -> list[dict]:
    """逐用例明细（排查用：id/status/路由/SQL/成本/错误）"""
    provider_cfg = app_config.llm.providers.get(meta["provider"], {})
    out = []
    for r in results:
        item = {"id": r.case.id, "query": r.case.query,
                "status": r.status, "error": r.error}
        if r.final_state:
            fs = r.final_state
            item.update({
                "intent": fs.get("intent"),
                "capability": fs.get("capability"),
                "capability_source": fs.get("capability_source"),
                "tool_calls": fs.get("tool_calls"),
                "sql": fs.get("sql"),
                "retrieved": {
                    "columns": [c.id for c in (fs.get("retrieved_column_infos") or [])],
                    "metrics": [m.name for m in (fs.get("retrieved_metric_infos") or [])],
                    "values": [v.value for v in (fs.get("retrieved_value_infos") or [])],
                },
                "retry_count": fs.get("retry_count", 0),
            })
        if r.tracker_summary:
            item["cost"] = compute_case_cost(r.tracker_records, r.tracker_summary,
                                             provider_cfg, meta.get("forced_tier"))
        out.append(item)
    return out


def render_json(results: list[CaseResult], dataset, meta: dict, aggregate: dict) -> Path:
    """JSON 报告（结构见 §3.7：meta / aggregate / cases）"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{meta['exp_label']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    payload = {"meta": meta, "aggregate": aggregate, "cases": _cases_detail(results, meta)}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.info(f"[eval] JSON 报告已写入 {path}")
    return path


def render_markdown(results: list[CaseResult], dataset, meta: dict, aggregate: dict) -> Path:
    """Markdown 报告（§3.7 五节：实验信息/质量/成本/环节/失败清单）"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{meta['exp_label']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    q, cost = aggregate["quality"], aggregate["cost"]
    lines: list[str] = []

    # ---- 1. 实验信息 ----
    lines += [f"# 评测报告：{meta['exp_label']}", "",
              "| 项 | 值 |", "|---|---|",
              f"| provider | {meta['provider']}（计费档位：{meta.get('pricing_tier', 'auto')}） |",
              f"| 数据集 | {meta['dataset_id']}（{meta['dataset_path']}） |",
              f"| 用例 | {meta['ok_cases']} ok / {meta['graph_error_cases']} graph_error / 共 {meta['total_cases']} |",
              f"| 开关覆盖 | {json.dumps(meta['feature_overrides'], ensure_ascii=False)} |",
              f"| flags 快照 | `{json.dumps(meta['flags_snapshot'], ensure_ascii=False)}` |",
              f"| 强制项 | {json.dumps(meta.get('forced', {}), ensure_ascii=False)} |",
              f"| git commit | {meta.get('git_commit') or '—'} |",
              f"| 耗时 | {meta['duration_s']}s |", ""]

    # ---- 2. 质量指标 ----
    lines += ["## 质量指标", ""]
    if q["retrieval"].get("disabled"):
        lines += ["- 检索：disabled（features.evaluation.retrieval_metrics=false）"]
    else:
        lines += ["### 检索（三通道宏平均）", "",
                  "| 通道 | hit@k | MRR | precision@k | recall@k |", "|---|---|---|---|---|"]
        for ch in ("columns", "metrics", "values", "tables"):
            m = q["retrieval"][ch]
            lines.append(f"| {ch} | {_fmt(m['hit_at_k'], True)} | {_fmt(m['mrr'], True)} | "
                         f"{_fmt(m['precision_at_k'], True)} | {_fmt(m['recall_at_k'], True)} |")
        lines.append("")
    intent = q["intent"]
    if intent.get("disabled"):
        lines += ["- 意图：disabled"]
    else:
        lines += [f"### 意图分类（support={intent['support']}）", "",
                  f"准确率：**{_fmt(intent['accuracy'], True)}**", ""]
        if intent["confusion_matrix"]:
            acts = sorted({a for row in intent["confusion_matrix"].values() for a in row})
            lines += ["| 预期\\实际 | " + " | ".join(acts) + " |", "|---" * (len(acts) + 1) + "|"]
            for exp, row in intent["confusion_matrix"].items():
                lines.append(f"| {exp} | " + " | ".join(str(row.get(a, 0)) for a in acts) + " |")
            lines.append("")
    tool = q["tool"]
    if tool.get("disabled"):
        lines += ["- 工具触发：disabled"]
    else:
        lines += [f"### 工具/能力触发（support={tool['support']}）", "",
                  f"invocation_accuracy：**{_fmt(tool['invocation_accuracy'], True)}**　"
                  f"recall：{_fmt(tool['tool_recall'], True)}　precision：{_fmt(tool['tool_precision'], True)}　"
                  f"漏调/误调/正确：{tool['confusion']['missed']}/{tool['confusion']['false_positive']}/"
                  f"{tool['confusion']['correct']}", ""]
    sql = q["sql"]
    if sql.get("disabled"):
        lines += ["- SQL：disabled"]
    else:
        lines += ["### SQL", "",
                  f"可执行率：**{_fmt(sql['executability'], True)}**（support={sql['support_exec']}）　"
                  f"结果正确性：**{_fmt(sql['correctness'], True)}**（support={sql['support_correct']}）　"
                  f"平均重试：{sql['avg_retries']:.2f}", ""]
    lines.append("")

    # ---- 3. 成本 ----
    lines += ["## 成本", ""]
    if cost.get("disabled"):
        lines += [f"disabled（{cost.get('note', '')}）", ""]
    else:
        lines += ["| 项 | 值 |", "|---|---|",
                  f"| 总费用 | {_fmt(cost['total_cost'])} 元 |",
                  f"| 每问费用 | {_fmt(cost['avg_cost_per_case'])} 元 |",
                  f"| token（输入/输出/合计） | {cost['tokens']['input']} / {cost['tokens']['output']} / {cost['tokens']['total']} |",
                  f"| 峰谷分布 | {json.dumps(cost['tier_distribution'], ensure_ascii=False)} |",
                  "", "> 口径：v1 一律按缓存未命中价核算（01 §7.2）", ""]

        # ---- 4. 环节成本（by_stage：成本花在哪）----
        lines += ["## 环节成本（by_stage）", "",
                  "| 环节 | 调用次数 | 输入 token | 累计延迟 ms |", "|---|---|---|---|"]
        for stage, s in sorted(cost["by_stage"].items(), key=lambda kv: -kv[1]["calls"]):
            lines.append(f"| {stage} | {s['calls']} | {s['input_tokens']} | {s['latency_ms']} |")
        lines.append("")

    # ---- 5. 失败清单 ----
    failures = [r for r in results if r.status != "ok"]
    lines += ["## 失败用例", ""]
    if not failures:
        lines += ["无"]
    else:
        for r in failures:
            lines.append(f"- `{r.case.id}`：{r.error}")
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"[eval] Markdown 报告已写入 {path}")
    return path


def _dig(payload: dict, *keys):
    """嵌套取值（任一层缺失返回 None）——compare 列提取用"""
    cur = payload
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


# compare 的列定义（§3.7：行=实验标签，列=关键指标）
_COMPARE_COLUMNS = (
    ("字段hit@k", lambda p: _dig(p, "aggregate", "quality", "retrieval", "columns", "hit_at_k"), True),
    ("字段MRR", lambda p: _dig(p, "aggregate", "quality", "retrieval", "columns", "mrr"), True),
    ("意图准确率", lambda p: _dig(p, "aggregate", "quality", "intent", "accuracy"), True),
    ("工具触发准确率", lambda p: _dig(p, "aggregate", "quality", "tool", "invocation_accuracy"), True),
    ("SQL可执行率", lambda p: _dig(p, "aggregate", "quality", "sql", "executability"), True),
    ("SQL正确性", lambda p: _dig(p, "aggregate", "quality", "sql", "correctness"), True),
    ("每问成本(元)", lambda p: _dig(p, "aggregate", "cost", "avg_cost_per_case"), False),
    ("总耗时(s)", lambda p: _dig(p, "meta", "duration_s"), False),
)


def compare_reports(paths: list[Path]) -> Path:
    """多报告并排对比（§3.7）：行=实验标签（含开关覆盖摘要），列=关键指标；
    输出同名 _compare.md + _compare.json"""
    reports = []
    for p in paths:
        payload = json.loads(Path(p).read_text(encoding="utf-8"))
        reports.append((p, payload))
    json_path = REPORTS_DIR / f"compare_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    md_path = json_path.with_suffix(".md")

    # JSON 对比报告：每份报告的 meta 摘要 + aggregate 全量（机器可算任意列）
    comparison = [{"report": str(p),
                   "exp_label": _dig(payload, "meta", "exp_label"),
                   "provider": _dig(payload, "meta", "provider"),
                   "feature_overrides": _dig(payload, "meta", "feature_overrides"),
                   "aggregate": payload.get("aggregate")}
                  for p, payload in reports]
    json_path.write_text(json.dumps(comparison, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")

    # Markdown 对比表
    lines = ["# 评测对比", "",
             "| 实验 | provider | 开关覆盖 | " +
             " | ".join(name for name, _, _ in _COMPARE_COLUMNS) + " |",
             "|---|---|---|" + "---|" * len(_COMPARE_COLUMNS)]
    for p, payload in reports:
        label = _dig(payload, "meta", "exp_label") or p.stem
        provider = _dig(payload, "meta", "provider") or "—"
        overrides = json.dumps(_dig(payload, "meta", "feature_overrides") or {}, ensure_ascii=False)
        cells = [_fmt(getter(payload), pct) for _, getter, pct in _COMPARE_COLUMNS]
        lines.append(f"| {label} | {provider} | {overrides} | " + " | ".join(cells) + " |")
    lines += ["", "> 生成自："] + [f"- {p}" for p, _ in reports]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"[eval] 对比报告已写入 {json_path} / {md_path}")
    return md_path
