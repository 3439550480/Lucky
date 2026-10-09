"""
失败文案人话化模块（v1.1 PRD FR-07）

链路位置：问数链路的两个失败出口都汇聚到这里 ——
  1. fail 节点（SQL 校验重试耗尽）→ build_fail_reply()
  2. run_sql 节点（执行异常，原样 re-raise 会把 pymysql 内部报错推给前端）
     → humanize_exec_error() 净化后重抛，QueryService 收到的已是安全文本

FR-07 业务规则：失败提示必须含三类信息（发生了什么 / 可能原因 / 建议怎么改）；
禁止输出堆栈与 SQL 原文（可输出脱敏后的 SQL 摘要）。纯函数、零状态，便于逐类单测。
"""
import re


def _sql_digest(sql: str, max_len: int = 80) -> str:
    """SQL 脱敏摘要：只保留 FROM/JOIN 涉及的表名（FR-07 规则 1 允许摘要、禁止原文）。

    why 不直接截断原文：SQL 原文可能暴露内部字段名/库结构；表名清单已足够
    帮用户定位"问错表"，且天然无敏感面。提取失败则不输出摘要（宁缺勿错）。
    """
    # step 1: 提取 FROM / JOIN 后的表名（含 db. 前缀），去重保序
    tables = re.findall(r"\b(?:from|join)\s+([`@\[\]\w.]+)", sql, flags=re.I)
    if not tables:
        return ""
    # step 2: 去反引号/方括号装饰，拼摘要并截断
    names = [t.strip("`[]@ ") for t in dict.fromkeys(tables)]
    digest = "涉及数据表：" + "、".join(names)
    return digest if len(digest) <= max_len else digest[:max_len] + "…"


def build_fail_reply(state: dict) -> str:
    """SQL 校验重试耗尽的最终失败文案（fail 节点调用）。

    三段式结构（FR-07）：发生了什么 / 可能原因 / 建议怎么改问法；末尾附表名摘要。
    """
    # step 1: 读取失败现场 —— 重试次数与最近一次校验错误（可能为空，容错处理）
    retries = state.get("retry_count", 0) or 0
    last_error = str(state.get("error") or "").strip()
    digest = _sql_digest(str(state.get("sql") or ""))
    # step 2: 三类信息组装 —— 原因从"校验错误"归纳为用户视角的典型情形，
    # 不照抄内部错误文本（那是排查线索不是用户语言）
    what = f"这个问题我尝试了 {retries + 1} 次自动修正，仍没能生成可执行的查询。"
    why = (
        "可能原因：\n"
        "· 问题涉及的指标或维度超出了当前数据范围（例如表外字段、跨库数据）\n"
        "· 问题表述较复杂，拆开后更容易被理解"
    )
    how = (
        "建议这样问：\n"
        "· 明确时间范围 + 指标 + 维度，如「2025 年 3 月各品类销量」\n"
        "· 把复合问题拆成几个简单问题分别提问"
    )
    if last_error:
        what += f"\n（最后一次校验未通过的原因：{last_error[:120]}）"
    # step 3: 拼接（摘要放最后，辅助定位但绝不展示 SQL 原文）
    reply = f"{what}\n\n{why}\n\n{how}"
    if digest:
        reply += f"\n\n（{digest}）"
    return reply


def humanize_exec_error(exc: Exception) -> str:
    """SQL 执行异常的人话化（run_sql 节点调用，净化后重抛）。

    按驱动报错关键词归类为四类典型情形，逐类给三类信息；未命中归为
    "服务内部异常"——只描述现象不搬运异常文本，杜绝内部细节泄露。
    """
    # step 1: 小写化匹配 —— 各驱动报错大小写不统一，统一小写后关键词归类
    low = str(exc).lower()
    # step 2: 关键词 → 三类信息映射（顺序即优先级，连接类最先判）
    if "connect" in low or "connection" in low or "can't reach" in low:
        return (
            "这次查询没能连上数据库。\n\n"
            "可能原因：数据服务正在维护或瞬时繁忙。\n\n"
            "建议：稍等几秒重试一次；若持续失败，请通过页面反馈入口告诉我。"
        )
    if "timeout" in low or "timed out" in low:
        return (
            "这次查询执行超时了。\n\n"
            "可能原因：问题涉及的统计范围太大（例如全年全量明细）。\n\n"
            "建议：缩小时间范围或增加筛选条件后重试，如「只看 2025 年 3 月」。"
        )
    if ("doesn't exist" in low or "unknown column" in low
            or "no such" in low or "unknown table" in low):
        return (
            "查询里用到的表或字段在当前数据范围内不存在。\n\n"
            "可能原因：问题涉及的维度超出了 demo 数据集范围。\n\n"
            "建议：换个已有维度提问（区域 / 品类 / 时间 / 会员等级），"
            "或参考示例问题改写。"
        )
    if "syntax" in low or "1149" in low:
        return (
            "生成的查询语句有误，自动修正也没能解决。\n\n"
            "可能原因：问题表述比较绕，模型理解出现了偏差。\n\n"
            "建议：把问题拆简单些再试，如「各区域销售额」而不是一句话问多个指标。"
        )
    # step 3: 兜底 —— 未识别异常只描述现象，绝不透传原始文本（内部路径/驱动细节阻断）
    return (
        "这次查询没能成功完成。\n\n"
        "可能原因：服务内部出现了一次临时异常。\n\n"
        "建议：换个更简单的问法重试；若同一问题反复失败，请反馈给我。"
    )
