/** Контракт с api.py. Правится вместе с ним — других потребителей у него нет. */

export type Channel = { kind: string; handle: string; suppressed?: boolean };

export type Lead = {
  company_id: string;
  name: string;
  city: string;
  domain: string | null;
  channel: Channel | null;
  why_now: string;
  quote: string | null;
  industry: string | null;
  from_model: boolean;
  intent_score: number;
  fit_score: number;
  sources: string[];
};

export type Signal = {
  type: string;
  observed_at: string;
  weight: number;
  quote: string | null;
  url: string | null;
};

export type BreakdownPart = {
  rule?: string;
  signal?: string;
  contribution: number;
  quote?: string;
  url?: string;
  observed_at?: string;
};

export type LeadDetail = Lead & {
  channels: Channel[];
  signals: Signal[];
  breakdown: BreakdownPart[];
};

export type Stats = {
  companies: number;
  with_intent: number;
  /** Потолок выдачи: компании с сигналом и рабочим каналом. Просить больше нечего. */
  available: number;
  suppressed: number;
  cities: string[];
};

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    // detail из HTTPException объясняет отказ словами («прогон не завершён»),
    // а код состояния — нет. Тела может и не быть: тогда остаётся код.
    const detail = await response.json().then((body) => body?.detail).catch(() => null);
    throw new Error(detail ?? `${init?.method ?? "GET"} ${url} — ${response.status}`);
  }
  return response.json();
}

export function fetchLeads(limit: number, city: string) {
  const query = new URLSearchParams({ limit: String(limit) });
  if (city) query.set("city", city);
  return json<{ leads: Lead[]; stats: Stats }>(`/api/leads?${query}`);
}

export function fetchLead(companyId: string) {
  return json<LeadDetail>(`/api/leads/${encodeURIComponent(companyId)}`);
}

export function refuse(handle: string, reason: string) {
  return json<{ handle: string; added: boolean }>("/api/suppression", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ handle, reason }),
  });
}

// ---- Продуктовые операции: пайплайны, джобы, события ----

export type Pipeline = { kind: string; title: string; steps: string[] };

export type Run = { run_id: number; started_at: string; finished_at: string | null;
  code_version: string | null; config_hash: string | null; note: string | null };

export type PipelineCatalogue = {
  pipelines: Pipeline[];
  operations: string[];
};

export function fetchCatalogue() {
  return json<PipelineCatalogue>("/api/pipeline");
}

export function startOperation(name: string) {
  return json<{ job: Job }>(`/api/operations/${encodeURIComponent(name)}`, { method: "POST" });
}

export function fetchRuns() {
  return json<{ runs: Run[] }>("/api/runs");
}

export function activateRun(runId: number) {
  return json<{ run_id: number }>(`/api/runs/${runId}/activate`, { method: "POST" });
}

export type JobStep = { name: string; command: string };

export type Job = {
  id: number;
  kind: string;
  title: string;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  step: number;
  steps: JobStep[];
  step_count: number;
  progress: { label?: string; current?: number; total?: number } | null;
  log_lines: number;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export type JobTail = { job: Job; lines: string[]; offset: number };

export function startPipeline(kind: string) {
  return json<{ job: Job }>(`/api/pipeline/${encodeURIComponent(kind)}`, { method: "POST" });
}

export function fetchJobs(limit = 20) {
  return json<{ jobs: Job[]; active: Job | null }>(`/api/jobs?limit=${limit}`);
}

export function fetchJob(id: number, offset = 0) {
  return json<JobTail>(`/api/jobs/${id}?offset=${offset}`);
}

export function cancelJob(id: number) {
  return json<{ job: Job }>(`/api/jobs/${id}/cancel`, { method: "POST" });
}

/** Счётчики всех трёх систем одним ответом — живая шапка дашборда. */
export type DashboardStats = {
  sourcing: Stats & { available: number };
  writer: { threads: number; drafts: number; sent: number; replies: number };
  sender: { status: string; numbers: Record<string, number> };
  jobs: { active: Job | null; recent: Job[] };
};

export function fetchStats() {
  return json<DashboardStats>("/api/stats");
}

/** Подписка на SSE. Сервер шлёт события snapshot | job | log | refresh;
 * на refresh консьюмер обычно перезабирает fetchStats().
 *
 * Стрим ходит на API-оригин напрямую, минуя rewrite next: dev-прокси
 * отдаёт браузеру заголовки, но буферизует тело бесконечного ответа —
 * события до страницы не доезжают. JSON-эндпоинтам это не мешает. */
const SSE_ORIGIN = process.env.NEXT_PUBLIC_API_ORIGIN ?? "";

export function subscribeEvents(
  onEvent: (event: MessageEvent) => void,
): () => void {
  const source = new EventSource(`${SSE_ORIGIN}/api/events`);
  for (const type of ["snapshot", "job", "log", "refresh"] as const) {
    source.addEventListener(type, onEvent as EventListener);
  }
  return () => source.close();
}
/** Ссылка, которой оператор реально открывает диалог. Текст не подставляем:
 *  первое сообщение пишется руками, в этом весь смысл ручной отправки в v1. */
export function channelLink(channel: Channel): string {
  const digits = channel.handle.replace(/\D/g, "");
  if (channel.kind === "whatsapp") return `https://wa.me/${digits}`;
  if (channel.kind === "phone") return `tel:+${digits}`;
  if (channel.kind === "email") return `mailto:${channel.handle}`;
  return channel.handle;
}

export const CHANNEL_LABELS: Record<string, string> = {
  whatsapp: "WhatsApp",
  phone: "Телефон",
  email: "Почта",
  website: "Сайт",
  instagram: "Instagram",
};

export type ThreadMessage = {
  role: "outgoing" | "incoming";
  text: string;
  angle: string | null;
  sent_at: string | null;
};

export type Conversation = {
  thread_id: string;
  channel_kind: string;
  messages: ThreadMessage[];
  draft: { message_id: number; draft_text: string; angle: string | null } | null;
  /** Агент советует не писать: нового повода в данных нет. Решает оператор. */
  stop: boolean;
};

export function fetchConversation(companyId: string) {
  return json<Conversation>(`/api/threads/${encodeURIComponent(companyId)}`);
}

/** Единственный вызов, который стоит денег: один ход = один запрос к модели. */
export function requestDraft(companyId: string, kind: "first" | "reply" | "followup") {
  return post<Conversation>(`/api/threads/${encodeURIComponent(companyId)}/draft`, { kind });
}

export function addIncoming(companyId: string, text: string) {
  return post<Conversation>(`/api/threads/${encodeURIComponent(companyId)}/incoming`, { text });
}

// ---- Система 2: инбокс тредов ----

export type ThreadSummary = {
  thread_id: string;
  company_id: string;
  company_name: string;
  created_at: string;
  sent: number;
  replies: number;
  drafts: number;
  last_at: string | null;
  last_message: string | null;
};

export function fetchThreads() {
  return json<{ threads: ThreadSummary[] }>("/api/threads");
}

// ---- Система 3: пул номеров ----

export type SenderNumber = {
  number: string;
  status: "new" | "warming" | "active" | "quarantined" | "banned";
  started_at: string;
  day: number;
  phase: "socket_delay" | "passive" | "internal" | "cold";
  daily_limit: number;
  sent_today: number;
  capacity: number;
  note: string | null;
};

export type SenderStatus = {
  status: string;
  autopilot: "off" | "replies" | "full";
  numbers: SenderNumber[];
  queue: { queued: number; sent_today: number; overdue: number };
  threads: { waiting: number; escalated: number };
  heartbeat: string | null;
};

export function fetchSender() {
  return json<SenderStatus>("/api/sender");
}

export type QueueRow = {
  outbox_id: number;
  message_id: number | null;
  thread_id: string | null;
  our_number: string;
  send_after: string;
  status: string;
  attempts: number;
  error: string | null;
  kind: string;
};

/** Кнопка оператора. Постановка в очередь живёт в системе 3: отправляет она же. */
export function queueMessage(threadId: string, text: string) {
  return post<QueueRow>("/api/sender/queue", { thread_id: threadId, text });
}

export function fetchQueue(threadId?: string) {
  const query = threadId ? `?thread_id=${encodeURIComponent(threadId)}` : "";
  return json<{ queue: QueueRow[]; recent: QueueRow[] }>(`/api/sender/queue${query}`);
}

export function setAutopilot(mode: "off" | "replies" | "full") {
  return post<{ autopilot: string }>("/api/sender/autopilot", { mode });
}

export async function registerNumber(number: string): Promise<SenderNumber> {
  return post("/api/sender/numbers", { number });
}

export async function pairNumber(number: string): Promise<{ code: string }> {
  return post(`/api/sender/numbers/${encodeURIComponent(number)}/pair`, {});
}

function post<T>(url: string, body: unknown) {
  return json<T>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export const SIGNAL_LABELS: Record<string, string> = {
  ads_platform: "Платит за рекламу",
  crm_widget: "CRM на сайте",
  inbound_widget: "Виджет входящих",
  service_catalog: "Каталог услуг с ценами",
};
