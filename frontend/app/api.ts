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
    throw new Error(`${init?.method ?? "GET"} ${url} — ${response.status}`);
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
  exit_code: number | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export type JobTail = { job: Job; lines: string[]; offset: number };

export function fetchPipelines() {
  return json<Pipeline[]>("/api/pipeline");
}

export function startPipeline(kind: string, limit = 10) {
  return json<{ job: Job }>(`/api/pipeline/${encodeURIComponent(kind)}?limit=${limit}`, {
    method: "POST",
  });
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
  sender: { status: string };
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

export function markSent(companyId: string, text: string) {
  return post<Conversation>(`/api/threads/${encodeURIComponent(companyId)}/sent`, { text });
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

// ---- Система 3: статус отправки ----

export type SenderStatus = { status: string; title: string; planned: string[] };

export function fetchSender() {
  return json<SenderStatus>("/api/sender");
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
  vacancy_sales: "Ищет продажников",
  vacancy_stale: "Вакансия висит давно",
};
