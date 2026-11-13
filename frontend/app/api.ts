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

export type Stats = {
  companies: number;
  with_intent: number;
  /** Потолок выдачи: компании с сигналом и рабочим каналом. Просить больше нечего. */
  available: number;
  suppressed: number;
  cities: string[];
};

/** Ошибка запроса вместе с кодом состояния: словами объясняет отказ `detail`,
 *  а отличить «состояние» (409) от «авария» (500) можно только по коду. */
export class ApiError extends Error {
  /** `code` — стабильный код доменной ошибки (`errors.cityInvalid`): по нему
   *  страница выбирает текст, по `detail` — нет. У старых ручек кода нет. */
  constructor(message: string, readonly status: number, readonly code?: string) {
    super(message);
    this.name = "ApiError";
  }
}

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    // detail из HTTPException объясняет отказ словами («прогон не завершён»),
    // а код состояния — нет. Тела может и не быть: тогда остаётся код.
    const body = await response.json().catch(() => null);
    const detail = typeof body?.detail === "string" ? body.detail : null;
    throw new ApiError(
      detail ?? `${init?.method ?? "GET"} ${url} — ${response.status}`,
      response.status,
      typeof body?.code === "string" ? body.code : undefined,
    );
  }
  return response.json();
}

export function fetchLeads(limit: number, city: string) {
  const query = new URLSearchParams({ limit: String(limit) });
  if (city) query.set("city", city);
  return json<{ leads: Lead[]; stats: Stats }>(`/api/leads?${query}`);
}

/** Отказ в той форме, в какой его хранит бэкенд: контакт уже приведён к
 *  канонической форме, `refused_at` — ISO 8601 в UTC. */
export type Refusal = { handle: string; reason: string; refused_at: string };

/** Повтор по тому же контакту не ошибка: бэкенд вернёт первую запись (200). */
export function refuse(handle: string, reason: string) {
  return json<Refusal>("/api/refusals", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ handle, reason }),
  });
}

// ---- Исследование: партия, лиды, карточка (модуль research) ----

export type SignalType =
  | "reviews_missed_lead"
  | "reviews_unanswered_complaint"
  | "site_hiring_sales"
  | "site_no_pricing"
  | "crm_widget"
  | "ads_platform"
  | "inbound_widget";

export const SIGNAL_LABELS: Record<SignalType, string> = {
  reviews_missed_lead: "Жалоба: не дозвонились или не ответили",
  reviews_unanswered_complaint: "Жалоба в отзыве без ответа",
  site_hiring_sales: "Ищут людей в продажи",
  site_no_pricing: "Нет цен на сайте",
  crm_widget: "CRM на сайте",
  ads_platform: "Платят за рекламу",
  inbound_widget: "Виджет входящих",
};

export type Contact = { kind: string; handle: string };

export type ResearchHook = {
  angle: string;
  quote: string;
  url: string;
  source: "reviews" | "site";
  observed_at: string;
};

export type Pain = {
  statement: string;
  evidence: string[];
  severity: "видно явно" | "предполагается" | "не видно";
};

export type ResearchDossier = {
  summary: string;
  what_they_do: string;
  hooks: ResearchHook[];
  pains: Pain[];
  approach: string;
  decision_maker_hint: string | null;
};

export type Contribution = {
  signal: SignalType;
  weight: number;
  age_days: number;
  contribution: number;
  quote: string;
  url: string;
  observed_at: string;
};

export type ResearchLead = {
  company_id: string;
  name: string;
  city: string;
  channel: Contact;
  score: number;
  why_now: ResearchHook;
  breakdown: Contribution[];
  dossier: ResearchDossier;
};

export type BatchStatus = "collecting" | "researching" | "done" | "stopped" | "interrupted";
export type ResearchStatus = "pending" | "researching" | "done" | "failed";

export type BatchCompany = {
  company_id: string;
  name: string;
  status: ResearchStatus;
  attempt: number | null;
  error: string | null;
  rejected_quotes: number;
  started_at: string | null;
  finished_at: string | null;
};

export type Batch = {
  batch_id: number;
  status: BatchStatus;
  size: number;
  stop_requested: boolean;
  error: string | null;
  started_at: string;
  finished_at: string | null;
  counts: Record<ResearchStatus, number>;
  companies: BatchCompany[];
};

export type ResearchAttempt = Omit<BatchCompany, "company_id" | "name"> & { batch_id: number };

export type CompanyResearch = {
  company_id: string;
  name: string;
  city: string;
  contacts: Contact[];
  channel: Contact | null;
  refused: boolean;
  lead: boolean;
  score: number;
  breakdown: Contribution[];
  why_now: ResearchHook | null;
  dossier: ResearchDossier | null;
  model: string | null;
  researched_at: string | null;
  versions: number;
  attempts: ResearchAttempt[];
};

export type ResearchSettings = {
  batch_size: number;
  model: string;
  weights: Record<SignalType, number>;
  half_life_days: number;
};

export function fetchResearchLeads() {
  return json<ResearchLead[]>("/api/research/leads");
}

/** `null` — партий ещё не было. */
export function fetchBatch() {
  return json<Batch | null>("/api/research/batches/current");
}

export function startBatch() {
  return json<Batch>("/api/research/batches", { method: "POST" });
}

export function stopBatch() {
  return json<Batch>("/api/research/batches/current/stop", { method: "POST" });
}

export function fetchCompanyResearch(companyId: string) {
  return json<CompanyResearch>(`/api/research/companies/${encodeURIComponent(companyId)}`);
}

export function researchAgain(companyId: string) {
  return json<Batch>(`/api/research/companies/${encodeURIComponent(companyId)}`, {
    method: "POST",
  });
}

export function fetchResearchSettings() {
  return json<ResearchSettings>("/api/research/settings");
}

export function saveResearchSettings(settings: ResearchSettings) {
  return json<ResearchSettings>("/api/research/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
}

/** Тексты ошибок исследования по `code`; неизвестный код — `detail` бэкенда. */
const RESEARCH_ERRORS: Record<string, string> = {
  "errors.batchRunning": "Партия уже идёт — дождитесь её или остановите.",
  "errors.batchNotRunning": "Партия уже не идёт — останавливать нечего.",
  "errors.modelKeyMissing":
    "Нет ключа модели: задайте OPENROUTER_API_KEY в backend/.env и перезапустите бэкенд.",
  "errors.companyRefused": "Компания в отказах — ей больше не пишем, исследовать незачем.",
  "errors.companyNotFound": "Такой компании нет в базе.",
};

export function researchError(failure: unknown) {
  const code = failure instanceof ApiError ? failure.code : undefined;
  return (code && RESEARCH_ERRORS[code]) || (failure instanceof Error ? failure.message : String(failure));
}

// ---- Компании: сегменты и их сбор из 2GIS ----

export type CatalogItem = { id: string; name: string };
export type SegmentSettings = { cities: string[]; rubrics: string[] };
export type SegmentStatus = "new" | "collecting" | "collected" | "failed";
export type SegmentState = {
  city: string;
  rubric: string;
  status: SegmentStatus;
  branches: number;
  companies: number;
  /** 2GIS показал меньше организаций, чем обещал: потолок пагинации. */
  capped: boolean;
  error: string | null;
  updated_at: string | null;
};
export type SegmentsView = {
  settings: SegmentSettings;
  cities: CatalogItem[];
  rubrics: CatalogItem[];
  segments: SegmentState[];
};

export function fetchSegments() {
  return json<SegmentsView>("/api/companies/segments");
}

/** Раздел сохраняется целиком: неизвестный город или id рубрики не из цифр —
 *  422 с `code` (`errors.cityInvalid`, `errors.rubricInvalid`). */
/** id города 2GIS → название: лиды и карточка несут id. */
export function fetchCityNames() {
  return fetchSegments().then(
    (view) => Object.fromEntries(view.cities.map((c) => [c.id, c.name])) as Record<string, string>,
  );
}

export function saveSegments(settings: SegmentSettings) {
  return json<SegmentsView>("/api/companies/segments", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
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

export type JobStep = {
  name: string;
  command: string;
  stage: number;
  lane: number;
  status: "pending" | "running" | "done" | "failed" | "cancelled";
  progress: { label?: string; current?: number; total?: number } | null;
};

export type Job = {
  id: number;
  kind: string;
  title: string;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  step: number;
  steps: JobStep[];
  step_count: number;
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
  sender: {
    status: string;
    numbers: Record<string, number>;
    // Необязательные не для красоты: metrics.sender_stats отдаёт "queue": {}
    // на базе без таблиц системы 3.
    queue: { queued?: number; sent_today?: number; overdue?: number };
    threads: { waiting: number; escalated: number };
    heartbeat: string | null;
  };
  jobs: { active: Job | null; recent: Job[] };
  /** Режим стенда решает бэкенд: без него сайдбар рисовал бы «Песочницу»,
   *  ручек которой в приложении нет. */
  sandbox: boolean;
};

export function fetchStats() {
  return json<DashboardStats>("/api/stats");
}

/** Что требуется от человека сейчас. Порядок задаёт бэкенд — по срочности. */
export type Task = {
  /** backup — без компании и треда: company_id и thread_id пустые. */
  kind: "escalated" | "draft" | "stuck" | "backup";
  company_id: string;
  company_name: string;
  thread_id: string;
  our_number: string | null;
  why: string;
  at: string | null;
};

export function fetchTasks(limit = 50) {
  return json<{ tasks: Task[] }>(`/api/tasks?limit=${limit}`);
}

/** Бэкап невосстановимого слоя в папку iCloud Drive (#8). */
export type BackupStatus = {
  enabled: boolean;
  running: boolean;
  target: string;
  last_at: string | null;
  snapshots: number;
};

export type BackupSnapshot = { name: string; taken_at: string; pages_added: number };

export function fetchBackup() {
  return json<BackupStatus>("/api/backup");
}

/** 409 errors.backupRunning — уже идёт; 503 errors.backupDisabled — папки нет. */
export function runBackup() {
  return json<BackupSnapshot>("/api/backup", { method: "POST" });
}

export type ActivityEvent = {
  at: string;
  last_at: string;
  repeats: number;
  actor: string;
  outcome: string;
  subject: string | null;
  detail: string | null;
};

export type ActivityWorker = {
  actor: string;
  last_at: string | null;
  events: number;
  /** Порог молчания приезжает с бэкенда: интервалы тиков живут в sender/config.toml. */
  silent_after_seconds: number;
};

export function fetchActivity(limit = 200, actor?: string) {
  const query = new URLSearchParams({ limit: String(limit) });
  if (actor) query.set("actor", actor);
  return json<{ events: ActivityEvent[]; workers: ActivityWorker[] }>(`/api/activity?${query}`);
}

/** Подписка на SSE. Сервер шлёт события snapshot | job | log | refresh | activity;
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
  for (const type of ["snapshot", "job", "log", "refresh", "activity"] as const) {
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
  message_id: number | null;
  /** Вид касания из outbox: cold | followup | reply. У входящих пуст. */
  kind: string | null;
};

export type Conversation = {
  thread_id: string;
  channel_kind: string;
  /** Статус ведёт система 3; null — треда ещё нет, черновик будет первым. */
  status: string | null;
  /** Почему статус такой. null у тредов, чей статус ставили до того, как
   *  причину начали сохранять. */
  status_reason: string | null;
  /** Наш номер, с которого лид видит переписку. Один на тред. */
  our_number: string | null;
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
  status: string;
  sent: number;
  replies: number;
  drafts: number;
  last_at: string | null;
  last_message: string | null;
  status_reason: string | null;
  our_number: string | null;
};

export function fetchThreads() {
  return json<{ threads: ThreadSummary[] }>("/api/threads");
}

/** Первые касания, ждущие проверки. Порядок задаёт бэкенд: по убыванию оценки
 *  research. Компания, которой research не знает, приходит под своим id без города. */
export type ColdDraft = {
  company_id: string;
  company_name: string;
  city: string | null;
  score: number;
  thread_id: string;
  message_id: number;
  draft_text: string;
  angle: string | null;
  model: string | null;
  has_prompt: boolean;
};

export function fetchColdDrafts() {
  return json<{ drafts: ColdDraft[] }>("/api/threads/drafts");
}

export function fetchPrompt(companyId: string, messageId: number) {
  return json<{ prompt: [string, string][] | null; model: string | null }>(
    `/api/threads/${encodeURIComponent(companyId)}/messages/${messageId}/prompt`,
  );
}

export type ThreadOutcome = "meeting_agreed" | "meeting_held" | "refused" | "lost";

export function setThreadOutcome(companyId: string, outcome: ThreadOutcome) {
  return json<{ outcome: string }>(
    `/api/threads/${encodeURIComponent(companyId)}/outcome`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ outcome }),
    },
  );
}

export type FunnelStep = { step: string; count: number };
export type Breakdown = { key: string; sent: number; replied: number; meetings: number };
export type AnalyticsReport = {
  days: number;
  funnel: FunnelStep[];
  by_offer: Breakdown[];
  by_angle: Breakdown[];
  by_segment: Breakdown[];
  edited_share: number;
  diagnosis: "ok" | "reply_rate_low" | "icp_mismatch";
};

export function fetchAnalytics(days = 30) {
  return json<AnalyticsReport>(`/api/analytics?days=${days}`);
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
  skip_warmup: number;
};

export type WarmupCalendarRow = {
  phase: "socket_delay" | "passive" | "internal" | "cold";
  days: string;
  daily_limit: string;
};

export type WarmupLogRow = {
  outbox_id: number;
  our_number: string;
  recipient: string;
  status: string;
  updated_at: string;
};

export type AutopilotMode = "off" | "manual" | "replies" | "full";

/** Порядок — от «ничего не уходит» к «уходит всё»: в нём режимы стоят на
 * переключателях «Отправки» и песочницы. */
export const AUTOPILOT_LABELS: Record<AutopilotMode, string> = {
  off: "Стоп",
  manual: "Ручной",
  replies: "Только ответы",
  full: "Полный автопилот",
};

export type SenderStatus = {
  status: string;
  autopilot: AutopilotMode;
  numbers: SenderNumber[];
  queue: { queued: number; sent_today: number; overdue: number };
  threads: { waiting: number; escalated: number };
  heartbeat: string | null;
  warmup_calendar: WarmupCalendarRow[];
  warmup_log: WarmupLogRow[];
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

export function setAutopilot(mode: AutopilotMode) {
  return post<{ autopilot: string }>("/api/sender/autopilot", { mode });
}

export async function registerNumber(number: string, skipWarmup = false): Promise<SenderNumber> {
  return post("/api/sender/numbers", { number, skip_warmup: skipWarmup });
}

/** Снятие карантина — оператор подтверждает, что причина (см. `note` номера)
 * больше не действует; сам монитор здоровья статус назад не откатывает. */
export async function liftQuarantine(number: string): Promise<SenderNumber> {
  return post(`/api/sender/numbers/${encodeURIComponent(number)}/status`, { status: "warming" });
}

/** Постфактум для уже существующего номера — та же отметка, что чекбокс
 * «уже прогрет» при регистрации. */
export async function markWarmed(number: string): Promise<SenderNumber> {
  return post(`/api/sender/numbers/${encodeURIComponent(number)}/warmed`, {});
}

export async function qrNumber(number: string): Promise<{ qr: string }> {
  return post(`/api/sender/numbers/${encodeURIComponent(number)}/qr`, {});
}

function post<T>(url: string, body: unknown) {
  return json<T>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// ---- Песочница: подменный транспорт и роль лида ----

export type SandboxRun = {
  run_id: string;
  company_id: string;
  created_at: string;
  warmed: boolean;
  offset_seconds: number;
  active: boolean;
};

export type SandboxFaults = {
  send: "ok" | "not_sent" | "unknown";
  delivery: "delivered" | "read" | "silent";
  number: "connected" | "loggedOut" | "stalled";
  has_whatsapp: boolean;
};

/** Судьба нашего сообщения в очереди. У входящего её нет: лид ничего не ждёт. */
export type SandboxFate = {
  status: string;
  attempts: number;
  send_after: string;
  delivered_at: string | null;
  read_at: string | null;
  error: string | null;
};

export type SandboxBubble = {
  message_id: number;
  role: "outgoing" | "incoming";
  kind: "draft" | "queued" | "sent" | "incoming";
  text: string;
  at: string;
  fate: SandboxFate | null;
};

export type SandboxChat = {
  thread_id: string;
  company_id: string;
  stage: string;
  status: string;
  touch_no: number;
  next_touch_at: string | null;
  bubbles: SandboxBubble[];
};

export function fetchSandboxRuns() {
  return json<{ runs: SandboxRun[]; now: string }>("/api/sandbox/runs");
}

export function createSandboxRun(companyId: string, warmed: boolean) {
  return post<{ run: SandboxRun }>("/api/sandbox/runs", {
    company_id: companyId,
    warmed,
  });
}

export function activateSandboxRun(runId: string) {
  return post<{ run: SandboxRun }>(
    `/api/sandbox/runs/${encodeURIComponent(runId)}/activate`,
    {},
  );
}

/** preset: jitter | hour | day | window. Что они значат, решает бэкенд. */
export function moveSandboxClock(preset: string) {
  return post<{ offset_seconds: number; now: string }>("/api/sandbox/clock", { preset });
}

export function fetchSandboxFaults() {
  return json<SandboxFaults>("/api/sandbox/faults");
}

export function setSandboxFaults(change: Partial<SandboxFaults>) {
  return post<SandboxFaults>("/api/sandbox/faults", change);
}

export function fetchSandboxChat() {
  return json<SandboxChat>("/api/sandbox/chat");
}

export function sendSandboxIncoming(text: string) {
  return post<{ handled: boolean }>("/api/sandbox/incoming", { text });
}
