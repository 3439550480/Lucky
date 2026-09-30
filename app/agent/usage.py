"""
用量采集模块（01 文档 §3.2）

链路位置：llm_factory.create_llm() 创建模型实例时，把 LLMUsageTracker 作为实例级
callback 挂载上去 —— LangChain 在每次 LLM 调用开始/结束/失败时回调对应钩子，
tracker 零侵入地记账。消费方有两个：
  1. QueryService：请求结束时调用 summary() 打日志（在线可观测）
  2. 03 评估 runner：逐用例收集 tracker 明细，喂给 cost_metrics.py（成本指标）
设计决策：采集用 callback 而非改节点代码 —— 节点只管调 chain.ainvoke，
计量对业务代码完全透明。
"""
import time
from dataclasses import dataclass
from typing import Optional

from langchain_core.callbacks import BaseCallbackHandler


@dataclass
class LLMCallRecord:
    """一次 LLM 调用的完整记账条目。

    token 字段可能为 None：OpenAI 兼容端点通常返回 usage，但个别厂商/错误响应
    不带 —— 缺失时成本计算降级为仅延迟统计，绝不因缺字段抛异常（01 §3.2 约束）
    """
    stage: str                       # 归属环节 = 最近祖先 chain 名（LangGraph 节点函数名）
    model: str                       # 模型名（如 deepseek-flash）
    input_tokens: Optional[int]      # usage_metadata.input_tokens
    output_tokens: Optional[int]
    total_tokens: Optional[int]
    latency_ms: int                  # 本次调用耗时（开始到结束）
    success: bool                    # False = on_llm_error 路径
    error: Optional[str]             # 失败时的异常摘要
    ts: float                        # 调用开始时间戳（03 成本模块按此判峰谷档位）
    prompt_cache_hit_tokens: Optional[int] = None   # [05 §3.4] DeepSeek 扩展字段，验证 KV cache 改造效果
    prompt_cache_miss_tokens: Optional[int] = None  # [05 §3.4] 其它 provider 恒为 None


class LLMUsageTracker(BaseCallbackHandler):
    """Request 级用量采集器（BaseCallbackHandler 子类）。

    生命周期：每个请求创建一个实例（create_llm 内），随请求结束而丢弃 ——
    评测时逐用例独立，互不污染（00 红线：用量采集用 callback 而非改节点）
    """

    def __init__(self, provider: str, model: str) -> None:
        # step 1: 保存实例元数据 —— summary() 与 03 报告标注用，callback 本身不读配置
        self.provider = provider
        self.model = model
        # step 2: 初始化记账容器（后续单位逐个启用的数据结构）
        self._records: list[LLMCallRecord] = []
        self._chain_names: dict = {}      # run_id -> 节点名（on_chain_start 维护）
        self._chain_parents: dict = {}    # run_id -> 父 run_id（_nearest_chain_name 上溯用）
        self._llm_starts: dict = {}       # run_id -> 开始时间戳（on_llm_start 维护）
        self._llm_stages: dict = {}       # run_id -> 归属环节名（on_llm_start 维护，on_llm_end 消费）

    # ---- LangChain 回调钩子（由 LangChain 运行时调用，业务代码不直接调用）----
    # on_chain_start 参数说明：serialized=序列化的 chain 信息；inputs=chain 输入；
    # run_id=本次 run 唯一 ID；parent_run_id=父 run ID（顶层为 None）；
    # tags/metadata=运行时附加上下文（可能含 LangGraph 节点信息）；**kwargs=向前兼容

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None,
                       tags=None, metadata=None, **kwargs) -> None:
        # step 1: 解析 chain 名 —— LangChain 版本差异下名字可能出现在两处，
        # 依次尝试：serialized["name"]（旧版）→ kwargs["name"]（新版 core 传参）
        # 旧版本里，serialized 通常是一个 dict，长这样：
        # serialized = {
        #     "name": "generate_sql",
        #     "id": ["langchain", "chains", "LLMChain"],
        #     ...
        # }
        name = None
        if isinstance(serialized, dict):
            name = serialized.get("name")
        if not name:
            name = kwargs.get("name")
        # step 1.5: LangGraph 场景优先取节点名 —— metadata 携带 langgraph_node（如 "generate_sql"），
        # 比 LCEL 包装层的注册名（RunnableSequence）更贴近 03 报告的"环节"语义
        if metadata and metadata.get("langgraph_node"):
            name = metadata["langgraph_node"]
        # step 2: 血缘登记对所有 run 生效 —— 匿名 LCEL 中间层若不登记父链接，
        # _nearest_chain_name 的上溯会在匿名层断链（验证中抓到的真 bug）；
        # 名字登记仅对有名字的 run 生效
        self._chain_parents[run_id] = parent_run_id
        if name:
            self._chain_names[run_id] = name

    def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs) -> None:
        # step 1: 出栈 —— 名字与血缘一并清理，防止长会话下匿名 run 累积撑大映射表；
        # 此时本节点的 LLM 子 run 已在 on_llm_start 完成归属解析，清理是安全的。
        # pop 带默认值，未登记的 run_id（匿名 chain）静默忽略，
        # 保证回调层永不抛异常（记账失败不能影响业务请求，01 §3.2 约束）
        self._chain_names.pop(run_id, None)
        self._chain_parents.pop(run_id, None)

    def _nearest_chain_name(self, parent_run_id) -> str:
        """沿父链向上找最近的已登记 chain 名（私有辅助，仅 on_llm_start 使用）。
        LangGraph 的 run 树是多层嵌套的：节点 run（有名）→ LCEL 中间 run（常无名）→ LLM run。
        LLM run 的直接父往往是匿名的 LCEL 组合器，所以要沿 parent_run_id 一路上溯，
        直到撞见登记过的节点名。上溯用的是 on_chain_start 顺手记录的 parent 链。"""
        # step 1: 从直接父 run 开始，沿 _chain_parents 向上走
        current = parent_run_id
        seen = set()
        # step 2: 环检测（seen 集合）—— 回调树理论上是无环的，但防御第三方代码构造环
        # 造成死循环；撞到已访问的 run_id 立即放弃
        while current is not None and current not in seen:
            seen.add(current)
            # step 3: 撞见登记过的名字 → 就是最近的祖先节点名
            if current in self._chain_names:
                return self._chain_names[current]
            # step 4: 没撞见 → 跳到该 run 的父 run 继续
            current = self._chain_parents.get(current)
        # step 5: 全链走完仍无名（如节点本身匿名/回调时序异常）→ "unknown"
        # 不抛异常、不猜测 —— 03 验收标准 5 允许 unknown 出现但要可排查
        return "unknown"

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None,
                     **kwargs) -> None:
        # step 1: 记录开始时间戳 —— latency 的起点；也是 03 成本模块判 DeepSeek
        # 峰谷档位的依据（按 ts 定价），所以存 time.time() 绝对时间戳而非差值
        self._llm_starts[run_id] = time.time()
        # step 2: 解析归属环节并缓存 —— 放在 start 而非 end 解析，是因为 chain_end
        # 出栈时祖先信息已被移除，start 时刻的调用栈才是完整的
        self._llm_stages[run_id] = self._nearest_chain_name(parent_run_id)


    # LLMResult(
    #     generations=[
    #         [   # 第 0 个 prompt 对应的一组候选结果
    #             ChatGeneration(   # 第 0 个候选
    #                 message=AIMessage(
    #                     content="...",
    #                     usage_metadata={...},
    #                     additional_kwargs={...},
    #                 )
    #             )
    #         ]
    #     ],
    #     llm_output={...},
    # )
    def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs) -> None:
        # step 1: 取回 start 时刻缓存的时间戳与归属环节 —— 若 start 缺失（异常时序），
        # 用当前时间兜底、环节记 "unknown"，绝不让记账中断
        started_at = self._llm_starts.pop(run_id, time.time())
        stage = self._llm_stages.pop(run_id, "unknown")
        latency_ms = int((time.time() - started_at) * 1000)
        # step 2: 从 response 提取 usage —— 标准路径是 response.generations[[0]][0].message
        # 的 usage_metadata（langchain 统一封装）；取不到就逐级容错为 None
        input_tokens = output_tokens = total_tokens = None
        cache_hit = cache_miss = None
        try:
            message = response.generations[0][0].message
            usage = getattr(message, "usage_metadata", None)
            # getattr 是 Python 的一个内置函数，用来安全地获取对象的属性。
            if usage:
                input_tokens = usage.get("input_tokens")
                output_tokens = usage.get("output_tokens")
                total_tokens = usage.get("total_tokens")
                # [05 §3.4] 缓存命中：标准层 input_token_details.cache_read（langchain 统一封装，
                # DeepSeek 的 prompt_cache_hit_tokens / OpenAI 的 cached_tokens 都映射到这里）
                cache_read = (usage.get("input_token_details") or {}).get("cache_read")
            # 兜底：DeepSeek 原始字段实测在 response_metadata.token_usage
            # （additional_kwargs.usage 为 None——2026-09-30 实测修正了原读取路径）
            if cache_read is None:
                rm = getattr(message, "response_metadata", {}) or {}
                tu = rm.get("token_usage") or {}
                cache_read = tu.get("prompt_cache_hit_tokens")
            # 命中数已知时，未命中 = 输入总量 - 命中（下限 0 防御异常值）
            if cache_read is not None:
                cache_hit = cache_read
                cache_miss = max((input_tokens or 0) - cache_read, 0)
        except (IndexError, AttributeError, TypeError):
            pass  # 结构不符合预期 → token 记 None，summary 的 note 会提示
        # step 3: 追加记账条目 —— 成功路径
        self._records.append(LLMCallRecord(
            stage=stage, model=self.model,
            input_tokens=input_tokens, output_tokens=output_tokens,
            total_tokens=total_tokens, latency_ms=latency_ms,
            success=True, error=None, ts=started_at,
            prompt_cache_hit_tokens=cache_hit,
            prompt_cache_miss_tokens=cache_miss,
        ))

    def on_llm_error(self, error, *, run_id, parent_run_id=None, **kwargs) -> None:
        # step 1: 取回时间戳与环节（与 on_llm_end 相同的容错策略）
        started_at = self._llm_starts.pop(run_id, time.time())
        stage = self._llm_stages.pop(run_id, "unknown")
        latency_ms = int((time.time() - started_at) * 1000)
        # step 2: 失败记账 —— token 全 None；error 摘要截断到 200 字符防止日志爆炸
        self._records.append(LLMCallRecord(
            stage=stage, model=self.model,
            input_tokens=None, output_tokens=None, total_tokens=None,
            latency_ms=latency_ms, success=False,
            error=str(error)[:200], ts=started_at,
        ))

    # ---- 对外查询接口（QueryService 与评估 runner 调用）----

    def records(self) -> list[LLMCallRecord]:
        # step 1: 返回只读副本 —— 防御性拷贝：调用方拿到的是新列表，
        # 但条目对象本身是共享引用（dataclass 不可变约束留待需要时加 frozen，
        # v1 以"不改"约定代替）
        return list(self._records)

    def summary(self) -> dict:
        # step 1: 聚合总体指标 —— success 与失败分开计数；
        # token 聚合时跳过 None 条目（失败调用/端点未返回），全 None 时聚合值为 None
        records = self._records
        token_fields = ("input_tokens", "output_tokens", "total_tokens")
        agg = {
            "provider": self.provider,
            "model": self.model,
            "calls": len(records),
            "errors": sum(1 for r in records if not r.success),
        }
        for f in token_fields:
            values = [getattr(r, f) for r in records if getattr(r, f) is not None]
            agg[f] = sum(values) if values else None
        # step 2: 延迟聚合不受 usage 缺失影响，恒可计算
        agg["total_latency_ms"] = sum(r.latency_ms for r in records)
        # step 2.5: [05 §3.4] 缓存命中统计 —— DeepSeek 扩展字段（其它 provider 恒 None → 不输出比率）
        hit_values = [r.prompt_cache_hit_tokens for r in records
                      if r.prompt_cache_hit_tokens is not None]
        miss_values = [r.prompt_cache_miss_tokens for r in records
                       if r.prompt_cache_miss_tokens is not None]
        agg["cache_hit_tokens"] = sum(hit_values) if hit_values else None
        agg["cache_miss_tokens"] = sum(miss_values) if miss_values else None
        if agg["cache_hit_tokens"] is not None and (agg["cache_hit_tokens"] + (agg["cache_miss_tokens"] or 0)) > 0:
            total_prompt = agg["cache_hit_tokens"] + (agg["cache_miss_tokens"] or 0)
            agg["cache_hit_rate"] = round(agg["cache_hit_tokens"] / total_prompt, 4)
        # step 3: 环节维度聚合 —— by_stage 是 03 报告"成本花在哪"的直接来源；
        # 同一 stage 聚合 calls/tokens/latency（修正重试会让同 stage 出现多次调用，属预期）
        by_stage = {}
        for r in records:
            bucket = by_stage.setdefault(r.stage, {"calls": 0, "input_tokens": 0, "latency_ms": 0})
            bucket["calls"] += 1
            bucket["latency_ms"] += r.latency_ms
            if r.input_tokens is not None:
                bucket["input_tokens"] += r.input_tokens
        agg["by_stage"] = by_stage
        # step 4: note 提示 —— 有调用但一个 token 都没拿到 → 说明端点没返回 usage
        # （或字段路径与实测不符），提示排查；正常时为 None
        agg["note"] = "端点未返回 usage，token 数据缺失" if (
            records and agg["total_tokens"] is None
        ) else None
        return agg
