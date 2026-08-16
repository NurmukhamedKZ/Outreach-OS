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

export type Command = { name: string; title: string; command: string };

/** Хвост лога запуска: `lines` — то, чего у клиента ещё нет, `code` — null, пока идёт. */
export type RunTail = {
  name: string | null;
  title: string;
  lines: string[];
  offset: number;
  code: number | null;
};

export function fetchCommands() {
  return json<Command[]>("/api/runs");
}

export function startRun(name: string, args: string) {
  const query = args ? `?args=${encodeURIComponent(args)}` : "";
  return json<{ name: string }>(`/api/runs/${encodeURIComponent(name)}${query}`, { method: "POST" });
}

export function fetchRunTail(offset: number) {
  return json<RunTail>(`/api/runs/current?offset=${offset}`);
}

export function stopRun() {
  return json<{ name: string }>("/api/runs/current/stop", { method: "POST" });
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

export const SIGNAL_LABELS: Record<string, string> = {
  ads_platform: "Платит за рекламу",
  crm_widget: "CRM на сайте",
  inbound_widget: "Виджет входящих",
  service_catalog: "Каталог услуг с ценами",
  vacancy_sales: "Ищет продажников",
  vacancy_stale: "Вакансия висит давно",
};
