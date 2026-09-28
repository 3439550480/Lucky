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

export type AgentEvent = ProgressEvent | ResultEvent | ErrorEvent | ExplanationEvent;


export type StepState = {
  step: string;
  status: ProgressStatus;
  updatedAt: number;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: number;
  status?: "streaming" | "done" | "error";
  steps?: StepState[];
  result?: unknown;
  explanation?: string;     // 助手消息的解释文本
  error?: string;
};
