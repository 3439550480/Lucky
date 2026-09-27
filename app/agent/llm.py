"""
[已废弃] LLM 模块级单例（01 文档 §4 #3）

旧代码用法 `from app.agent.llm import llm` 已被多模型架构取代：
  - 节点内：llm = runtime.context["llm"]        （经 DataAgentContext 按请求注入）
  - 应用层：from app.agent.llm_factory import create_llm
本文件保留为"防呆兼容层"：任何遗漏迁移的引用会立即得到带迁移指引的 RuntimeError，
而不是静默用错模型。llm_factory.py 就绪后本文件不再提供任何可用的模型实例。
"""
import asyncio
import os

from app.agent.llm_factory import create_llm


def __getattr__(name: str):
    """模块级属性拦截（PEP 562）：仅拦截 llm 这个历史名字，其余属性照常报 AttributeError"""
    if name == "llm":
        raise RuntimeError(
            "llm 模块级单例已移除（01 文档多模型改造）："
            "节点内请使用 runtime.context['llm']；"
            "应用层请使用 from app.agent.llm_factory import create_llm"
        )
    raise AttributeError(f"module 'app.agent.llm' has no attribute '{name}'")


if __name__ == "__main__":
    # 本地自测：工厂直接创建默认模型，验证连通性（替代旧单例的 invoke 自测）
    llm = create_llm()

    async def _main():
        resp = await llm.ainvoke("用一句话介绍你自己")
        print(resp.content)

    asyncio.run(_main())
