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

  const reload = useCallback(() => {
    fetchSender().then(setStatus).catch(() => undefined);
  }, []);

  useEffect(reload, [reload]);

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
          onClick={() => registerNumber(number).then(reload)}
          disabled={!number}
        >
          Добавить в пул
        </button>
        <button
          className="btn"
          onClick={() => pairNumber(number).then((body) => setCode(body.code))}
          disabled={!number}
        >
          Получить код привязки
        </button>
        {code && (
          <p className="mono">
            Введите на телефоне: WhatsApp → Связанные устройства → Привязка по коду → {code}
          </p>
        )}
      </section>
    </>
  );
}
