# prompts/

提示词目录（2026-10-09 清空旧电商/奶茶场景提示词，安踏场景于 S3 重写后回填）。

## S3 需重写清单（对照被删除的 17 个文件）

**主链路（11）**

- [ ] `capability_route.prompt` —— 能力路由（安踏换装 + 多轮消解规则，缺陷①）
- [ ] `generate_sql.prompt` —— SQL 生成（安踏场景 + 条件继承条款，缺陷②）
- [ ] `correct_sql.prompt` —— SQL 修正
- [ ] `explain_result.prompt` —— 结果解释
- [ ] `default_answer.prompt` —— 兜底回复（default 定位待详谈后定稿）
- [ ] `filter_table_info.prompt` —— 表过滤
- [ ] `filter_metric_info.prompt` —— 指标过滤
- [ ] `extend_keywords_for_column_recall.prompt` —— 字段召回关键词扩展
- [ ] `extend_keywords_for_metric_recall.prompt` —— 指标召回关键词扩展
- [ ] `extend_keywords_for_value_recall.prompt` —— 取值召回关键词扩展
- [ ] `system_prompt.prompt` —— 系统前缀（build_system_prefix 消费）

**legacy 对照组（6，context_management=false 路径，KV cache 对照实验机制）**

- [ ] `legacy/capability_route.prompt`
- [ ] `legacy/generate_sql.prompt`
- [ ] `legacy/correct_sql.prompt`
- [ ] `legacy/explain_result.prompt`
- [ ] `legacy/default_answer.prompt`
- [ ] `memory_extract.prompt` —— 记忆提取（主链路用，legacy 无独立文件）

> 纪律：重写完成后 `context_management=false` 必须 = 旧行为可运行（Feature Flags 关闭语义恢复）。
