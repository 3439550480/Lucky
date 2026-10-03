# 验收记录（07_acceptance §6.2）

| 用例/实验 | 日期 | commit | 结果 | 备注 |
|----------|------|--------|------|------|
| S1 全默认冒烟 | 2026-10-02 | bc73006 前 | 通过 | eval_quick 10 用例全绿（意图/工具/SQL 全 100%） |
| S2 capability_routing=false | 2026-10-03 | 7c3986d 后 | 通过 | 在线断言：闲聊 query 32 帧 SSE 全部 capability=dataquery；runner 意图 90%（闲聊强制进问数=预期旧行为）、SQL 100% |
| S3 rules_fast_path=false | 2026-10-03 | 7c3986d 后 | 通过 | 意图 100%（embedding/LLM 接管）、SQL 100% |
| S4 embedding_route=false | 2026-10-03 | 7c3986d 后 | 通过 | 意图 100%、SQL 100% |
| S5 纯 LLM 分类 | 2026-10-03 | 7c3986d 后 | 通过 | 10 ok、意图 100% |
| S6 context_management=false | 2026-10-03 | d17a52e 后 | 通过 | 意图/SQL 100%（正确性不变，仅无 cache 优化） |
| S7 memory.long_term=true | 2026-10-03 | 0cc8a43 | 通过 | eval_memory：store/retrieval/recall/persistence 全 1.0；by_stage 含 memory_extract |
| S8 usage_tracking=false | 2026-10-03 | 7c3986d 后 | 通过 | 在线断言：LLM usage 日志 0 条，链路行为正常 |
| S9 评估子模块全关 | 2026-10-03 | 0cc8a43 后 | 通过 | 报告全区域 disabled、runner 完成 exit 0 |
| S10 非法布尔值 | 2026-10-03 | 0cc8a43 后 | 通过（修复后） | 初测暴露静默转 False 的 gap → apply_feature_overrides 严格解析修复，非法值现报明确错误 exit 1 |
| §4.1 /api/models | 2026-10-03 | 7c3986d 后 | 通过 | 3 provider 分组正确、default=deepseek |
| §4.1 SSE 帧核对 | 2026-10-03 | 7c3986d 后 | 通过 | 32 帧 progress/explanation 完整、capability 注入、无裸异常 |
| 50 用例全量基线 | 2026-10-03 | 00e244f 后 | 通过 | 50 ok；意图/工具 100%、SQL 可执行 100%/正确 97.4%（1 例真实语义 bug → BUG-01 已修复） |
| M5 KV cache 实测 | 2026-09-30 | d17a52e 前 | 通过 | 同会话第二轮 hit 2688 tokens（命中率 35%，命中量=前缀区体积） |
| eval_quick 回归（M5 开启） | 2026-10-02 | bc73006 | 通过 | 意图/工具/SQL 全 100%，BUG-01 哨兵通过 |

## 矩阵期间发现并修复

1. **S10 gap**：apply_feature_overrides 对非法布尔串（"abc"）静默转 False → 严格解析（true/false/1/0/yes/no 之外报错）
2. **S9 命令教训**：argparse 的 action="append" 参数每个值都需要 --features 前缀
3. 运行期 Docker 引擎两次自动关闭 → runner 容错（graph_error 不中断）经实战验证
