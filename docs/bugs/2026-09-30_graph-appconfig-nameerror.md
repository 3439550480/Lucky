# BUG-2026-09-30-02 · graph.py 缺失 app_config 导入（SQL 重试分支 NameError）

> 状态：**已修复关闭**　|　发现方式：评估框架（baseline_50_fix 回归）　|　严重度：高（SQL 首次校验失败时整条请求崩溃）

## 1. 基本信息

| 项 | 值 |
|---|---|
| 发现用例 | `dq_repeat_purchase_rate`（"2025年的复购率是多少"） |
| 发现批次 | baseline_50_fix_20260930_145632（BUG-01 修复回归时暴露） |
| 错误 | `NameError: name 'app_config' is not defined` → graph_error，请求级崩溃 |
| 修复 | `app/agent/graph.py` 补 `from app.conf.app_config import app_config` |

## 2. 根因

`graph.py:124` 的 SQL 重试分支读取 `app_config.sql.max_retries`，但模块头部没有导入 `app_config`。该分支**只在首次 `validate_sql` 失败时进入**——上一轮 50 用例全部一次通过校验，所以地雷未爆；本轮复购率用例（SQL 最复杂）首次校验失败，触发 NameError。

## 3. 为什么评估能抓到

这是典型的**低频路径缺陷**：主路径（一次通过）永远测试不到重试分支。50 条异构用例中总会有 SQL 需要重试——评估的规模化执行天然覆盖了这类分支，而手工点几次页面几乎碰不到。

## 4. 教训

- 图改造/重构后，**条件分支代码是导入遗漏的重灾区**（只在特定运行时路径执行，冒烟测不到）
- 评估框架的隐藏价值：不只是"量化质量"，还是**全路径的随机压力测试**——50 条异构用例 ≈ 50 次不同代码路径的组合采样
