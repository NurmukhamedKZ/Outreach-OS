"use client";

/** Отказ — единственное, что возвращается в систему от человека (PRD F21).
 *  Причина обязательна: список никогда не очищается, и через полгода объяснить
 *  запись будет некому. Вынесен из LeadCard: форма нужна и на «Холодных»,
 *  и в карточке лида. */

import { useState } from "react";
import { refuse, type Channel } from "@/app/api";

export default function RefusalForm({
  channel,
  onDone,
}: {
  channel: Channel | null;
  onDone: () => void;
}) {
  const [reason, setReason] = useState("");
  const [sending, setSending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [shown, setShown] = useState(channel?.handle ?? null);

  // Конвейер «Холодных» переиспользует форму на следующей компании. Причина
  // отказа уходит в suppression, который не очищается: перенести недописанный
  // текст про прошлую компанию на новую значит записать туда неправду.
  if (shown !== (channel?.handle ?? null)) {
    setShown(channel?.handle ?? null);
    setReason("");
    setFailure(null);
  }

  if (!channel) return null;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setSending(true);
    setFailure(null);
    try {
      await refuse(channel!.handle, reason.trim());
      setReason("");
      onDone();
    } catch (error) {
      setFailure((error as Error).message);
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="refusal">
      <form onSubmit={submit}>
        <input
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder={`Причина отказа для ${channel.handle}`}
          minLength={3}
          required
        />
        <button type="submit" disabled={sending || reason.trim().length < 3}>
          Больше не писать
        </button>
      </form>
      <p className="note">
        Запись уходит в <code className="mono">state.suppression</code> и переживает
        пересборку базы. Отменить нельзя — список не очищается.
      </p>
      {failure && <p className="failure">{failure}</p>}
    </div>
  );
}
