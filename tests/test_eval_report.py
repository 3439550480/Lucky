"""
报告场判定单测（[S3 分场] 用例通过口径 + 通过线判定）

纯离线：无 LLM / 无 DB / 不执行图，只验证"用例算不算通过""这场算不算达标"的口径——
这两个判定是八场通过线的分子与结论，口径错了整场结论就错（且没人看得出来）。
运行：PYTHONPATH=. uv run python tests/test_eval_report.py
"""
from types import SimpleNamespace

from app.evaluation.report import case_verdict, compute_scene_verdict


def _result(status="ok", assertions=None, error=None, case_id="c1"):
    """最小用例结果替身（case_verdict 只读 status/assertions/error/case.id）"""
    return SimpleNamespace(status=status, assertions=assertions, error=error,
                           case=SimpleNamespace(id=case_id))


def test_case_verdict_rules():
    # ① 有失败机制断言 → 不通过（图跑通了也不算：机制断言是本次升级的硬门）
    failed = case_verdict(_result(assertions=[{"key": "expect_sql_tables", "passed": False}]))
    assert failed[0] is False and "expect_sql_tables" in failed[1], failed
    # ② 图跑通且无失败断言 → 通过（未声明断言的普通用例按链路跑通计）
    assert case_verdict(_result())[0] is True
    # ③ 拒绝路径：graph_error 但有断言为真 → 通过（F4 登录即拒不得被算成失败）
    assert case_verdict(_result(status="graph_error", error="LoginRejected: disabled",
                                assertions=[{"key": "expect_login_rejected",
                                             "passed": True}]))[0] is True
    # ④ 无任何证据的 graph_error → 不通过（不谎报通过）
    assert case_verdict(_result(status="graph_error", error="boom"))[0] is False


def test_scene_verdict_pass_line():
    ds = SimpleNamespace(scene="F4 权限矩阵场", pass_line=0.9, dataset_id="f4")
    v = compute_scene_verdict([_result(case_id="a"),
                               _result(case_id="b", status="graph_error", error="boom")], ds)
    assert v["case_pass_rate"] == 0.5 and v["verdict"] is False, v
    assert v["failed_cases"] == [{"id": "b", "reason": "boom"}], v
    # 未设通过线 → verdict=None（不判达标、不输出 0 分）；场名缺省退回 dataset_id
    legacy = SimpleNamespace(scene="", pass_line=None, dataset_id="eval_anta")
    v2 = compute_scene_verdict([_result()], legacy)
    assert v2["verdict"] is None and v2["scene"] == "eval_anta" and v2["case_pass_rate"] == 1.0


def test_scene_skipped_not_counted():
    """跳过例不进分子分母："没跑"≠"没过"（F8 记忆组在 memory.long_term=false 时全 skipped）
    无有效例 → rate=None，verdict=None（不判达标，也不谎报 0 分）；部分跳过 → 分母只算跑了的。"""
    ds = SimpleNamespace(scene="F8 常规回归场（记忆组）", pass_line=1.0,
                         dataset_id="eval_anta_memory")
    v = compute_scene_verdict([_result(case_id="m1", status="skipped"),
                               _result(case_id="m2", status="skipped")], ds)
    assert (v["total"], v["counted"], v["skipped"]) == (2, 0, 2), v
    assert v["case_pass_rate"] is None and v["verdict"] is None, v
    v2 = compute_scene_verdict([_result(case_id="a"),
                                _result(case_id="m1", status="skipped")], ds)
    assert (v2["counted"], v2["skipped"]) == (1, 1), v2
    assert v2["case_pass_rate"] == 1.0 and v2["verdict"] is True, v2


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")     # Windows 控制台默认 GBK，中文输出会乱码
    test_case_verdict_rules()
    test_scene_verdict_pass_line()
    test_scene_skipped_not_counted()
    print("报告场判定单测：3/3 通过")
