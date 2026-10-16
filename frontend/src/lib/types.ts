export type AuthKind = "loading" | "guest" | "user";

export type Mode =
  | "job"
  | "project"
  | "general"
  | "interview"
  | "resume"
  | "jd"
  | "plan"
  | "supervisor"
  | "graph"
  | "mcp";

export type Step = {
  id?: string;
  tool: string;
  input: string;
  observation: string;
  status?: string;
};

export type Approval = {
  approval_id: string;
  request_id: string;
  run_id: string;
  tool_call_id: string;
  tool: string;
  arguments: Record<string, unknown>;
  status: "pending";
  expires_at: number;
};

export type MetaEvent = {
  event: string;
  data: string;
};

export type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  streaming?: boolean;
  steps?: Step[];
  approval?: Approval | null;
  approvalSubmitting?: boolean;
  approvalError?: string;
  approvalUnavailable?: boolean;
  meta?: MetaEvent[];
};

export type QuotaStatus = {
  identity: string;
  roles: string[];
  authenticated: boolean;
  question_limit: number;
  token_limit: number;
  used_questions: number;
  used_tokens: number;
  remaining_questions: number | null;
  remaining_tokens: number | null;
  reset_at: string | null;
};

export type TokenResponse = {
  access_token: string;
  token_type: string;
};

export type JobAnalysis = {
  summary: string;
  matched_skills: string[];
  missing_skills: string[];
  interview_questions: string[];
  study_plan: string[];
};
