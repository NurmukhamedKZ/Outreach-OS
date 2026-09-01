"use client";

/** Черновик и всё, что с ним делать: править, поставить в очередь, переписать,
 *  а главное — увидеть полный запрос, ушедший в модель. Общий для «Холодных»
 *  (первое касание) и «Диалогов» (follow-up/ответ): различие — только в том,
 *  что делает страница по onQueued/onRegenerate. */

import { useState } from "react";
import { fetchPrompt, queueMessage, requestDraft, type QueueRow } from "@/app/api";

const WHEN = new Intl.DateTimeFormat("ru", {
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
});

export default function MessageComposer({
  companyId,
  messageId,
  threadId,
  text,
  angle,
  model,
  hasPrompt,
  draftKind = "first",
  onQueued,
  onRegenerate,
}: {
  companyId: string;
  messageId: number;
  threadId: string;
  text: string;
  angle: string | null;
  model: string | null;
  hasPrompt: boolean;
  /** Первое касание и follow-up строятся разными задачами; ответ лида пишет
   *  агент-продавец, и его промпт здесь не сохраняется. */
  draftKind?: "first" | "followup" | "reply";
  onQueued: () => void;
  onRegenerate: () => void;
}) {
  const [value, setValue] = useState(text);
  const [prompt, setPrompt] = useState<[string, string][] | null>(null);
  const [promptAsked, setPromptAsked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [queued, setQueued] = useState<QueueRow | null>(null);
  const [shown, setShown] = useState(messageId);

  // Показанный черновик сменился — сбрасываем всё, что относилось к прежнему.
  // useState(text) отрабатывает только при монтировании, а конвейер «Холодных»
  // и переключение тредов в «Диалогах» переиспользуют этот же компонент: без
  // сброса в поле остался бы текст предыдущей компании, и «Отправить» ушло бы
  // с ним в чужой тред. Сброс живёт здесь, а не key= на вызывающей стороне,
  // потому что забыть его может только один файл, а не каждый следующий.
  if (shown !== messageId) {
    setShown(messageId);
    setValue(text);
    setQueued(null);
    setPrompt(null);
    setPromptAsked(false);
    setFailure(null);
  }

  // Промпт весит 2–4 КБ и нужен только тому, кто раскрыл
  // <details>: не грузим его при показе композера.
  async function openPrompt() {
    if (promptAsked) return;
    setPromptAsked(true);
    try {
      const stored = await fetchPrompt(companyId, messageId);
      setPrompt(stored.prompt);
    } catch (error) {
      setFailure((error as Error).message);
    }
  }

  async function send() {
    setBusy(true);
    setFailure(null);
    try {
      setQueued(await queueMessage(threadId, value));
      onQueued();
    } catch (error) {
      setFailure((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function regenerate() {
    setBusy(true);
    setFailure(null);
    try {
      await requestDraft(companyId, draftKind);
      onRegenerate();
    } catch (error) {
      setFailure((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="composer">
      <textarea
        rows={8}
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder="Текст сообщения — правится здесь, в историю уйдёт отправленный вариант"
      />
      <p className="composer-meta mono">
        {angle ?? "без угла"}
        {model ? ` · ${model}` : ""}
      </p>

      <details className="prompt-details" onToggle={openPrompt}>
        <summary>Полный запрос в модель</summary>
        {!hasPrompt ? (
          <p className="note">
            {draftKind === "reply"
              ? "собран агентом, см. Langfuse"
              : "черновик написан до того, как промпт начали сохранять"}
          </p>
        ) : prompt ? (
          <div className="prompt-block mono">
            {prompt.map(([role, content], index) => (
              <div key={index} className="prompt-pair">
                <span className="prompt-role">{role}</span>
                <pre>{content}</pre>
              </div>
            ))}
          </div>
        ) : (
          <p className="note">грузим…</p>
        )}
      </details>

      <div className="composer-actions">
        <button className="btn" disabled={busy || !value.trim() || Boolean(queued)} onClick={send}>
          {queued ? "В очереди" : "Отправить"}
        </button>
        <button className="btn-secondary" disabled={busy} onClick={regenerate}>
          Перегенерировать · стоит денег
        </button>
      </div>

      {queued && (
        <p className="note">
          Уйдёт с номера <span className="mono">{queued.our_number}</span> не раньше{" "}
          {WHEN.format(new Date(queued.send_after))}. В историю треда сообщение попадёт после
          подтверждения отправки.
        </p>
      )}
      {failure && <p className="failure">{failure}</p>}
    </section>
  );
}
