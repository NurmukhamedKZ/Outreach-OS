"use client";

/** Система 3: отправка. Пул номеров с днём прогрева и остатком дневного
 * лимита. Содержимое по-прежнему целиком приезжает с бэкенда (GET /api/sender),
 * страница не знает ни календаря прогрева, ни порогов.
 */

import { useCallback, useEffect, useState } from "react";
import { fetchSender, pairNumber, registerNumber, type SenderStatus } from "../api";

const PHASE_LABEL: Record<string, string> = {
  socket_delay: "сокет не привязан",
  passive: "только входящие",
  internal: "внутренний прогрев",
  cold: "боевые касания",
};

export default function Sender() {
  const [status, setStatus] = useState<SenderStatus | null>(null);
  const [code, setCode] = useState<string | null>(null);
  const [number, setNumber] = useState("");
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(() => {
    fetchSender().then(setStatus).catch(() => undefined);
  }, []);

  useEffect(reload, [reload]);

  /** Дубликат номера и лежащий Node приезжают сюда ошибкой запроса: без этого
   * кнопка молча ничего не делает, а в консоли висит unhandled rejection. */
  const report = (failure: unknown) => setError(String(failure));

  return (
    <>
      <header className="page-head">
        <h1>Отправка</h1>
        <span className="page-sub">
          система 3 · автопилот: {status?.autopilot ?? "…"}
        </span>
      </header>

      <section className="card">
        <h2>Пул номеров</h2>
        <table className="table">
          <thead>
            <tr>
              <th>Номер</th><th>Статус</th><th>День</th><th>Фаза</th>
              <th>Сегодня</th><th>Осталось</th>
            </tr>
          </thead>
          <tbody>
            {(status?.numbers ?? []).map((row) => (
              <tr key={row.number}>
                <td className="mono">{row.number}</td>
                <td>{row.status}</td>
                <td>{row.day}</td>
                <td>{PHASE_LABEL[row.phase] ?? row.phase}</td>
                <td>{row.sent_today} / {row.daily_limit}</td>
                <td>{row.capacity}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card">
        <h2>Подключить номер</h2>
        <input
          className="input mono"
          placeholder="+77001112233"
          value={number}
          onChange={(event) => setNumber(event.target.value)}
        />
        <button
          className="btn"
          onClick={() => {
            setError(null);
            registerNumber(number).then(reload).catch(report);
          }}
          disabled={!number}
        >
          Добавить в пул
        </button>
        <button
          className="btn"
          onClick={() => {
            setError(null);
            pairNumber(number).then((body) => setCode(body.code)).catch(report);
          }}
          disabled={!number}
        >
          Получить код привязки
        </button>
        {error && <p className="error-line">{error}</p>}
        {code && (
          <p className="mono">
            Введите на телефоне: WhatsApp → Связанные устройства → Привязка по коду → {code}
          </p>
        )}
      </section>
    </>
  );
}
