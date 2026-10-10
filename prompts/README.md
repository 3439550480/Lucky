# prompts/

提示词目录（2026-10-09 清空旧电商/奶茶场景提示词；2026-10-10 安踏场景 S3 全部回填完成）。

## S3 重写清单（对照被删除的 17 个文件）

**主链路（11）**

- [x] `capability_route.prompt` —— 能力路由（安踏换装 + 多轮消解规则，缺陷①）—— S3a 完成
- [x] `generate_sql.prompt` —— SQL 生成（安踏场景 + 条件继承条款，缺陷②）—— S3b 完成
- [x] `correct_sql.prompt` —— SQL 修正（三区结构 + 领域口径约束）
- [x] `explain_result.prompt` —— 结果解释（结论先行 + 数字保真）
- [x] `default_answer.prompt` —— 兜底回复（安踏定位 + 引导回问数）
- [x] `filter_table_info.prompt` —— 表过滤（JSON 对象契约 + sku 关联提示）
- [x] `filter_metric_info.prompt` —— 指标过滤（JSON 数组契约）
- [x] `extend_keywords_for_column_recall.prompt` —— 字段召回关键词扩展
- [x] `extend_keywords_for_metric_recall.prompt` —— 指标召回关键词扩展
- [x] `extend_keywords_for_value_recall.prompt` —— 取值召回关键词扩展
- [x] `system_prompt.prompt` —— 系统前缀（build_system_prefix 消费，`__CAPABILITIES__` 占位 + 三工具定义；PREFIX_VERSION 递增 v2）

**legacy 对照组（6，context_management=false 路径，KV cache 对照实验机制）**

- [x] `legacy/capability_route.prompt`
- [x] `legacy/generate_sql.prompt`（旧混排结构 + 安踏领域规则段）
- [x] `legacy/correct_sql.prompt`
- [x] `legacy/explain_result.prompt`
- [x] `legacy/default_answer.prompt`（旧版原样复刻——对照基线不做场景引导）
- [x] `memory_extract.prompt` —— 记忆提取（主链路用，四条硬约束保留、语境安踏化）

**2.0 新增（不在原 17 清单内）**

- [x] `replenish_plan.prompt` —— replenish 能力解释词（LLM 只叙事不碰数字）—— S3b 完成

> 纪律：重写完成后 `context_management=false` 必须 = 旧行为可运行（Feature Flags 关闭语义恢复）。
> 占位符与各消费方 input_variables 逐一对齐，验证脚本见 git 历史（tests 阶段随 S4 回归覆盖）。
