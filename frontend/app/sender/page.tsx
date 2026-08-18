"use client";

/** Система 3: отправка. Раздел закрыт, содержимое честно берётся с бэкенда
 * (GET /api/sender), а не хардкодом: когда система появится, страница
 * оживёт без правок фронта.
 */

import { useEffect, useState } from "react";
import { CheckCircleIcon, LockSimpleIcon } from "@phosphor-icons/react";
import { fetchSender, type SenderStatus } from "../api";

export default function Sender() {
  const [status, setStatus] = useState<SenderStatus | null>(null);

  useEffect(() => {
    fetchSender().then(setStatus).catch(() => undefined);
  }, []);

  return (
    <>
      <header className="page-head">
        <h1>Отправка</h1>
        <span className="page-sub">система 3 · в разработке</span>
      </header>

      <section className="card soon-hero">
        <span className="badge-pill">
          <LockSimpleIcon size={12} /> скоро
        </span>
        <h2>Письма дойдут, а не пропадут в спаме</h2>
        <p>
          Домены для холодных писем, прогрев ящиков и расписание рассылки. Первые
          две системы соберут и напишут черновики, эта доставит их адресатам.
        </p>
        <ul className="soon-list">
          {(status?.planned ?? []).map((item) => (
            <li key={item}>
              <CheckCircleIcon size={15} /> {item}
            </li>
          ))}
        </ul>
      </section>
    </>
  );
}
