/**
 * 智能体类型定义
 * 定义问数智能体前端使用的 SSE 事件、流程步骤和聊天消息类型
 */
// ==== 平台上下文类型（02 文档 §3.3）====
export type ModelInfo = {
  name: string;        // provider 名，作为请求体 model 值与下拉 value
  model: string;       // 物理模型名，用于 title 提示
  group: string;       // 分组名（下拉 optgroup）
};

export type ModelsResponse = {
  default: string;
  providers: ModelInfo[];
};

export type CapabilityInfo = {
  name: string;        // 能力名，作为请求体 capability 值与芯片 value
  description: string; // 芯片提示
};

export type CapabilitiesResponse = {
  capabilities: CapabilityInfo[];
};

// ==== SSE 事件协议（00 §4.3：各事件追加可选 capability 字段）====
export type ProgressStatus = "running" | "success" | "error";

export type ProgressEvent = {
  type: "progress";
  step: string;
  status: ProgressStatus;
  capability?: string;   // [NEW] 路由后事件携带，前端按 capability×type 分发
};

export type ResultEvent = {
  type: "result";
  data: unknown;
  capability?: string;   // [NEW]
};

export type ErrorEvent = {
  type: "error";
  message: string;
  capability?: string;   // [NEW]
};

export type ExplanationEvent = {
  type: "explanation";
  text: string;
  capability?: string;   // [NEW]
};

// [2.0 S3b] 补货计划事件：replenish 能力的结构化建议（LLM 解释仍走 explanation 事件）
export type ReplenishRow = {
  sku_id: string;
  product_name: string;
  category_l1: string;
  avg_daily_sales: number;
  coverage_days: number;
  safety_days: number;
  available_qty: number;
  on_transit: number;
  target_qty: number;
  suggest_qty: number;
  days_left: number | null;
};

export type ReplenishEvent = {
  type: "replenish";
  plan: ReplenishRow[];
  total_gap: number;         // 缺口 SKU 总数
  total_suggest_qty: number; // 合计建议补货件数
  scope: string[];           // 品类范围（空 = 全店）
  capability?: string;
};

// [S3 结构化确认] 出入库确认门事件：前端渲染「确认提交 / 取消」按钮卡片，
// 点击走独立端点 POST /api/flow/confirm —— 服务端不再解析"确认/取消"自由文本
export type ConfirmEvent = {
  type: "confirm";
  action: string;                          // 待确认动作（v1: inventory_write）
  slots: Record<string, string | number>;  // 槽位快照（direction/doc_no/sku_id/qty）
  text: string;                            // 摘要文案（前端写入 content，卡片只渲染按钮）
  capability?: string;
};

export type AgentEvent =
  | ProgressEvent
  | ResultEvent
  | ErrorEvent
  | ExplanationEvent
  | ReplenishEvent
  | ConfirmEvent;


export type StepState = {
  step: string;
  status: ProgressStatus;
  updatedAt: number;
};

// [S3 结构化确认] 挂在助手消息上的"待确认动作"（confirm 事件写入）
export type ConfirmRequest = {
  action: string;
  slots: Record<string, string | number>;
  text: string;
  decided?: "confirm" | "cancel";  // 点击后置位 → 按钮禁用（防重复提交）
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: number;
  status?: "streaming" | "done" | "error";
  steps?: StepState[];
  result?: unknown;
  replenish?: ReplenishRow[]; // [2.0 S3b] 补货计划明细（replenish 事件写入）
  confirm?: ConfirmRequest;   // [S3] 待确认动作（点击后 decided 置位）
  explanation?: string;     // 助手消息的解释文本
  error?: string;
};
