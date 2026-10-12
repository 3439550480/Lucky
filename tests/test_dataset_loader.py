"""
评测集加载器合同单测（[R1] staff_id / [R3] 流程序列与写库断言 / [R4] 超长会话构造器）

纯离线：无 LLM / 无 DB / 不执行图，只验证"JSON 写错就不让过"——用例真跑起来才发现的
配置错误（拼错断言字段、序列错位、doc_no 类型不对）成本极高（一次全量运行要跑图+查库），
故把口径钉在加载期。运行：PYTHONPATH=. uv run python tests/test_dataset_loader.py
"""
import json
import tempfile
from pathlib import Path

from app.evaluation.dataset import load_dataset


def _load(case_item: dict, **extra):
    """把单条用例包成临时评测集并加载（校验失败会在加载期抛 ValueError）

    extra：顶层字段覆盖（如 scene="F4 权限矩阵场", pass_line=0.9）
    """
    tmp = Path(tempfile.mkdtemp(prefix="eval_ds_"))
    path = tmp / "ds.json"
    raw = {"dataset_id": "t", "version": "1.0", "cases": [{"enabled": True, **case_item}]}
    raw.update(extra)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return load_dataset(path)


def _reject_reason(case_item: dict, **extra) -> str:
    """期望被加载器拒绝：返回报错文案；若加载通过则直接失败（不静默）"""
    try:
        _load(case_item, **extra)
    except ValueError as e:
        return str(e)
    raise AssertionError(f"该用例应被加载器拒绝，实际放行：{case_item}")


def test_r3_sequences_accept_and_reject():
    # Given 2 轮对话 + 逐轮注入序列, When 加载, Then 放行（元素形态合法，未注入轮用 None）
    ds = _load({"id": "c1", "dialogue": ["给 15262011 入库 5 件", "确认提交"],
                "slots_sequence": [{"direction": "in", "qty": 5}, None],
                "confirm_sequence": [None, "accept"]})
    assert ds.cases[0].confirm_sequence == [None, "accept"]
    # 确认值只认 accept/cancel：写 "yes" 会被静默忽略 → 用例假通过（流程没被确认却全绿）
    assert "仅支持 accept/cancel" in _reject_reason(
        {"id": "c2", "dialogue": ["入库 5 件", "确认"], "confirm_sequence": [None, "yes"]})
    # 序列比轮数长 = 错位（注入不到那一轮）→ 拒绝
    assert "长度 ≤ 轮数(2)" in _reject_reason(
        {"id": "c3", "dialogue": ["入库 5 件", "确认"], "slots_sequence": [{}, {}, {}]})
    # 槽位元素必须是 dict（写字符串会注入出怪 state）
    assert "必须为 dict" in _reject_reason(
        {"id": "c4", "dialogue": ["入库 5 件", "确认"], "slots_sequence": ["direction=in"]})


def test_expect_flow_doc_shape():
    # committed 缺省 = true（"提交生效"是常态，取消/幂等重复才需显式声明 false）
    ds = _load({"id": "c1", "query": "确认提交", "expect_flow_doc": {"doc_no": "RK1"}})
    assert ds.cases[0].expect_flow_doc == {"doc_no": "RK1"}
    # doc_no 是查流水依据：写成数字 → 查空 → 假失败（"真记了账"却被判没记），必须拒绝
    assert "expect_flow_doc" in _reject_reason(
        {"id": "c2", "query": "确认提交", "expect_flow_doc": {"doc_no": 2026101301}})
    # committed 必须是 bool：写 "false" 字符串会被当"给了值"却语义不明
    assert "expect_flow_doc" in _reject_reason(
        {"id": "c3", "query": "确认提交",
         "expect_flow_doc": {"doc_no": "RK1", "committed": "false"}})


def test_session_filler_requires_pool():
    # Given turns>0 但无 pool, When 加载, Then 拒绝——否则填充轮无处取话术，历史不涨（R4 假通过）
    assert "pool 必填" in _reject_reason(
        {"id": "c1", "query": "销售额多少", "session_filler": {"turns": 24}})
    # turns=0 允许无 pool（纯占位，不灌轮次）
    ds = _load({"id": "c2", "query": "销售额多少", "session_filler": {"turns": 0}})
    assert ds.cases[0].session_filler == {"turns": 0}
    # turns 非 int → 拒绝
    assert "session_filler" in _reject_reason(
        {"id": "c3", "query": "销售额多少", "session_filler": {"turns": "24", "pool": ["闲聊"]}})


def test_staff_id_type():
    # Given 工号字符串, When 加载, Then 原样带出（runner 据此查 dim_staff 组装 StaffIdentity）
    ds = _load({"id": "c1", "query": "本店补货建议", "staff_id": "S001"})
    assert ds.cases[0].staff_id == "S001"
    # 工号写成数字 → 查不到员工 → 角色段渲染不出 → 断言口径漂移，必须拒绝
    assert "staff_id 必须为 str" in _reject_reason(
        {"id": "c2", "query": "本店补货建议", "staff_id": 1})


def test_unknown_expect_field_rejected():
    # Given 拼错断言字段名（真名 expect_flow_doc）, When 加载, Then 立即报错
    # 不报错的后果：既跳过类型校验、也不被 EvaluationCase 接收 → 该用例静默变"无断言"→ 全绿
    assert "未定义" in _reject_reason(
        {"id": "c1", "query": "确认提交", "expect_flow_committed": {"doc_no": "RK1"}})
    # 白名单内字段不受影响；元数据键（notes/tags）不在守备范围，仍可自由书写
    ds = _load({"id": "c2", "query": "确认提交", "expect_permission_denied": "inventory.write",
                "notes": "F3 权限用例", "tags": ["r2"]})
    assert ds.cases[0].expect_permission_denied == "inventory.write"


def test_disabled_case_counted_not_loaded():
    # 骨架占位机制：enabled=false 只计数不进 cases（否则占位用例会以"无断言"混进分母）
    ds = _load({"id": "c1", "query": "占位", "enabled": False})
    assert ds.cases == [] and ds.disabled_count == 1


def test_scene_and_pass_line():
    # [S3 分场] 场名 + 通过线（0~1）正常带出
    ds = _load({"id": "c1", "query": "占位"}, scene="F4 权限矩阵场", pass_line=0.9)
    assert ds.scene == "F4 权限矩阵场" and ds.pass_line == 0.9
    # 缺省 → 不分场 / 不判达标（存量数据集结构不变）
    legacy = _load({"id": "c2", "query": "占位"})
    assert legacy.scene == "" and legacy.pass_line is None
    # 写成 90（想表达 90%）→ 拒绝：否则"通过率 ≥90"永真，达标判定形同虚设
    assert "pass_line" in _reject_reason({"id": "c3", "query": "占位"}, pass_line=90)
    assert "pass_line" in _reject_reason({"id": "c4", "query": "占位"}, pass_line="0.9")


def test_all_datasets_load():
    """仓库内全部数据集必须能加载（数据集也是配置：场名/通过线写错要立刻炸，别等跑完一小时）"""
    dataset_dir = Path(__file__).resolve().parents[1] / "evaluation" / "datasets"
    files = sorted(dataset_dir.glob("*.json"))
    assert files, f"未找到任何数据集：{dataset_dir}"
    for path in files:
        ds = load_dataset(path)
        print(f"  {path.name:30s} scene={ds.scene or '（未分场）':14s} "
              f"pass_line={ds.pass_line} cases={len(ds.cases)} disabled={ds.disabled_count}")


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")     # Windows 控制台默认 GBK，中文输出会乱码
    test_r3_sequences_accept_and_reject()
    test_expect_flow_doc_shape()
    test_session_filler_requires_pool()
    test_staff_id_type()
    test_unknown_expect_field_rejected()
    test_disabled_case_counted_not_loaded()
    test_scene_and_pass_line()
    print("已有的场数据集清单（evaluation/datasets/）：")
    test_all_datasets_load()
    print("评测集加载器合同单测：8/8 通过")
