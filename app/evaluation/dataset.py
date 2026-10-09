"""
评测集加载与校验（03 文档 §3.5）

职责：JSON → 强类型 dataclass，错误带定位信息抛 ValueError（§2.1：不静默跳过）
被检字段口径：expected.columns 格式与 ColumnInfo.id 一致（表名.字段名），
加载器不做格式强制（§7.1——避免过度耦合，匹配不到时指标自然为 0）
"""
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class EvaluationCase:
    id: str
    query: str
    capability: str = "dataquery"      # 缺省视为 dataquery（§2.1）
    expected: dict = field(default_factory=dict)   # intent/tools/columns/metrics/values/tables，缺省项为空
    golden_sql: str | None = None      # 缺省则不计入 SQL 正确性（仍计入可执行率）
    match_mode: str = "exact"          # 继承 defaults
    k: int = 5                         # 继承 defaults
    notes: str = ""
    memory_setup: dict | None = None   # [06] 基础回忆用例：{"setup_runs": [...], "expected_recall": [...], "expect_note": bool}
    dialogue: list[str] | None = None  # [v1.1] 多轮对话用例：逐轮同 thread_id 顺序执行（SParC/CHASE 范式）；
                                       # expected/golden_sql 只对最后一轮评估，历史经 context_store 自然承接


@dataclass
class EvaluationDataset:
    dataset_id: str
    version: str
    description: str
    cases: list[EvaluationCase]        # 仅含 enabled=True；disabled 仅计数
    disabled_count: int = 0


def _err(line_hint: str, msg: str) -> ValueError:
    return ValueError(f"[评测集校验失败] {line_hint}: {msg}")


def load_dataset(path: Path) -> EvaluationDataset:
    """json.load + 全量校验。校验规则见 03 文档 §2.1 字段表"""
    # step 1: 文件存在性与 JSON 合法性
    if not path.is_file():
        raise _err(str(path), "评测集文件不存在")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise _err(str(path), f"JSON 解析失败：{e}") from e

    # step 2: 顶层字段
    dataset_id = raw.get("dataset_id")
    if not dataset_id:
        raise _err(str(path), "缺少必填字段 dataset_id")
    defaults = raw.get("defaults") or {}
    match_mode = defaults.get("match_mode", "exact")
    if match_mode not in ("exact", "contains"):
        raise _err(str(path), f"defaults.match_mode 非法：{match_mode}（仅 exact/contains）")
    k = defaults.get("k", 5)
    if not isinstance(k, int) or k <= 0:
        raise _err(str(path), f"defaults.k 必须为正整数，实际：{k!r}")

    # step 3: 逐用例校验（定位 = cases 数组下标，报错含用例 id 双保险）
    cases: list[EvaluationCase] = []
    seen_ids: set[str] = set()
    disabled_count = 0
    for i, item in enumerate(raw.get("cases") or []):
        hint = f"cases[{i}]"
        case_id = item.get("id")
        if not case_id:
            raise _err(hint, "缺少必填字段 id")
        if case_id in seen_ids:
            raise _err(hint, f"用例 id 重复：{case_id}")
        seen_ids.add(case_id)
        if not item.get("query") and not item.get("dialogue"):
            raise _err(hint, f"用例 {case_id} 缺少必填字段 query（多轮用例可用 dialogue 替代）")
        if item.get("enabled") is None:
            raise _err(hint, f"用例 {case_id} 缺少必填字段 enabled（骨架占位机制依赖它）")
        expected = item.get("expected") or {}
        # expected 各键类型校验：集合类字段必须为 list[str]
        for list_key in ("columns", "metrics", "values", "tables", "tools"):
            v = expected.get(list_key)
            if v is not None and (not isinstance(v, list) or not all(isinstance(x, str) for x in v)):
                raise _err(hint, f"用例 {case_id} 的 expected.{list_key} 必须为 list[str]")
        if "intent" in expected and not isinstance(expected["intent"], str):
            raise _err(hint, f"用例 {case_id} 的 expected.intent 必须为 str")
        # [06] memory_setup 类型校验（基础回忆用例）
        mem = item.get("memory_setup")
        if mem is not None:
            if not isinstance(mem, dict):
                raise _err(hint, f"用例 {case_id} 的 memory_setup 必须为 dict")
            for list_key in ("setup_runs", "expected_recall"):
                v = mem.get(list_key)
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    raise _err(hint, f"用例 {case_id} 的 memory_setup.{list_key} 必须为 list[str]")
        # [v1.1] dialogue 多轮校验：非空 list[str]，且与 query 至少有一者存在
        dlg = item.get("dialogue")
        if dlg is not None:
            if not isinstance(dlg, list) or not dlg or not all(isinstance(x, str) and x.strip() for x in dlg):
                raise _err(hint, f"用例 {case_id} 的 dialogue 必须为非空 list[str]")
            if not item.get("query"):
                item["query"] = dlg[-1]           # target = 最后一轮（query 缺省取末轮，兼容下游）
            if len(dlg) < 2:
                raise _err(hint, f"用例 {case_id} 的 dialogue 至少需要 2 轮（单轮请直接用 query）")

        if not item["enabled"]:
            disabled_count += 1
            continue                                    # disabled 不进 cases，仅计数
        cases.append(EvaluationCase(
            id=case_id, query=item["query"],
            capability=item.get("capability") or "dataquery",
            expected=expected,
            golden_sql=item.get("golden_sql"),
            match_mode=item.get("match_mode", match_mode),
            k=item.get("k", k),
            notes=item.get("notes", ""),
            memory_setup=item.get("memory_setup"),
            dialogue=dlg,
        ))

    return EvaluationDataset(
        dataset_id=dataset_id,
        version=raw.get("version", "1.0"),
        description=raw.get("description", ""),
        cases=cases,
        disabled_count=disabled_count,
    )
