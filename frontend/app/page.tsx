"use client";

/** Обзор: три системы одним экраном. Запуск — контекстный (на «Лидах» и
 *  «Холодных»), монитор джобы — в полоске наверху и на «Процессах», здесь
 *  только счётчики и последние запуски.
 *
 *  Полминуты. Ничего не крутится зря: если джоба идёт, её видно из любого
 *  места; если нет, видна история последних.
 */

import { useLive } from "@/components/live";

const WHEN = new Intl.DateTimeFormat("ru", { hour: "2-digit", minute: "2-digit" });

export default function Overview() {
  const { stats, jobs, connected } = useLive();

  return (
    <>
      <header className="page-head">
        <h1>Обзор</h1>
        <span className="page-sub">
          {stats
            ? `${stats.sourcing.available} лидов готовы к работе`
            : connected
              ? "загружаем счётчики…"
              : "ждём бэкенд"}
        </span>
      </header>

      <Guide />

      {stats ? (
        <div className="metrics">
          <section className="metric-card">
            <div className="metric-head">Сбор лидов</div>
            <div className="metric-row is-total">
              <span>компаний в базе</span>
              <b>{stats.sourcing.companies}</b>
            </div>
            <div className="metric-row">
              <span>с intent-сигналами</span>
              <b>{stats.sourcing.with_intent}</b>
            </div>
            <div className="metric-row">
              <span>с рабочим каналом</span>
              <b>{stats.sourcing.available}</b>
            </div>
            <div className="metric-row">
              <span>отказов (suppression)</span>
              <b>{stats.sourcing.suppressed}</b>
            </div>
          </section>

          <section className="metric-card">
            <div className="metric-head">Персонализация</div>
            <div className="metric-row is-total">
              <span>тредов открыто</span>
              <b>{stats.writer.threads}</b>
            </div>
            <div className="metric-row">
              <span>черновиков ждёт</span>
              <b>{stats.writer.drafts}</b>
            </div>
            <div className="metric-row">
              <span>отправлено сообщений</span>
              <b>{stats.writer.sent}</b>
            </div>
            <div className="metric-row">
              <span>ответов от лидов</span>
              <b>{stats.writer.replies}</b>
            </div>
          </section>

          <section className="metric-card">
            <div className="metric-head">Отправка</div>
            <div className="metric-row is-total">
              <span>номеров в пуле</span>
              <b>{totalNumbers(stats.sender)}</b>
            </div>
            <div className="metric-row">
              <span>активных</span>
              <b>{stats.sender.numbers.active ?? 0}</b>
            </div>
            <div className="metric-row">
              <span>греется</span>
              <b>{stats.sender.numbers.warming ?? 0}</b>
            </div>
          </section>
        </div>
      ) : (
        <div className="metrics">
          <div className="metric-card skeleton" aria-hidden>
            <div className="skeleton-line" style={{ width: "60%" }} />
            <div className="skeleton-line" style={{ width: "90%" }} />
            <div className="skeleton-line" style={{ width: "75%" }} />
          </div>
          <div className="metric-card skeleton" aria-hidden>
            <div className="skeleton-line" style={{ width: "80%" }} />
            <div className="skeleton-line" style={{ width: "55%" }} />
          </div>
          <div className="metric-card skeleton" aria-hidden>
            <div className="skeleton-line" style={{ width: "70%" }} />
            <div className="skeleton-line" style={{ width: "40%" }} />
          </div>
        </div>
      )}
      <section className="card">
        <h3>История запусков</h3>
        <div className="jobs-list">
          {jobs.map((job) => (
            <div key={job.id} className="jobs-row">
              <span>
                <span className="jobs-title">{job.title}</span>
                {job.error ? <span className="jobs-when"> · {job.error}</span> : null}
              </span>
              <span className="jobs-when">{job.finished_at ? WHEN.format(new Date(job.finished_at)) : "…"}</span>
              <span className={`status is-${job.status}`}>
                {job.status === "running"
                  ? `шаг ${job.step + 1} из ${job.step_count}`
                  : STATUS[job.status] ?? job.status}
              </span>
            </div>
          ))}
          {jobs.length === 0 && <p className="note">Пока ничего не запускали.</p>}
        </div>
      </section>
    </>
  );
}

/** Гайд: путь оператора от пустой базы до отправленного сообщения.
 * Собран из настоящих подписей кнопок — это инструкция, а не пересказ архитектуры.
 */
function Guide() {
  return (
    <details className="guide card">
      <summary>Как этим пользоваться</summary>

      <ol className="guide-steps">
        <li>
          <b>Найти компании.</b> Сайдбар → <span className="ui">Лиды</span> →{" "}
          <span className="ui">Поиск новых лидов</span> — 2ГИС, сайты, инстаграм,
          пересборка базы. Ход виден сверху в полоске и подробно — на «Процессах»:
          названные шаги, счётчик и живой лог; остановить — <span className="ui">прервать</span>.
          Очередь одна: пока джоба идёт, остальные кнопки заблокированы.
        </li>
        <li>
          <b>Понять, кто из них годится.</b> Кнопка <span className="ui">Анализ и досье</span>:
          модель читает отзывы, сайт и инстаграм и ставит intent-скор. Нужен{" "}
          <code className="mono">OPENROUTER_API_KEY</code> в <code className="mono">backend/.env</code>,
          иначе шаг упадёт с 503.
        </li>
        <li>
          <b>Посмотреть выдачу.</b> Сайдбар → <span className="ui">Лиды</span>. Слева список
          по скору (фильтры «все города» и «N лидов» сверху), клик по строке — карточка:
          разбор скора, досье модели, каналы, сигналы, сырьё и ответы модели. Не писать этой
          компании никогда — в карточке впишите причину и нажмите{" "}
          <span className="ui">Больше не писать</span>; запись переживает пересборку
          и не отменяется.
        </li>
        <li>
          <b>Написать первое сообщение.</b> Сайдбар → <span className="ui">Холодные</span> →{" "}
          <span className="ui">Черновики топ-N</span>: агент напишет тем, у кого треда ещё нет.
          Черновик правится прямо в поле, полный запрос в модель открывается под{" "}
          <span className="ui">Полный запрос в модель</span>, дальше{" "}
          <span className="ui">Отправить</span> — в историю треда сообщение попадёт после
          подтверждения отправки. Ответ, пришедший мимо системы, вставляется как есть кнопкой{" "}
          <span className="ui">Записать ответ</span> на «Диалогах».
        </li>
        <li>
          <b>Подключить номер.</b> Сайдбар → <span className="ui">Отправка</span> → блок
          «Подключить номер»: введите <code className="mono">+77001112233</code>,{" "}
          <span className="ui">Добавить в пул</span>, затем <span className="ui">Показать QR</span> и
          на телефоне WhatsApp → Связанные устройства → Привязать устройство. Номер уже отлежался —
          галочка «уже прогрет» при добавлении или <span className="ui">Уже прогрет</span> в таблице
          пула. Новый номер сам идёт по календарю прогрева, пишет нашим же номерам.
        </li>
        <li>
          <b>Включить отправку.</b> Там же, в блоке «Очередь», три кнопки:{" "}
          <span className="ui">Стоп</span> (kill switch, прогрев продолжается),{" "}
          <span className="ui">Только ответы</span> (автомат отвечает написавшим, сам не пишет),{" "}
          <span className="ui">Полный автопилот</span> (сам ставит касания в очередь). Окно
          отправки 10:00–18:00 Asia/Almaty, ответы уходят круглосуточно.
        </li>
        <li>
          <b>Следить.</b> На «Отправке» — «созрели, но стоят», «ждут ответа», «эскалировано» и
          пульс воркера (если пульс замер — воркер лёг). Ответы лидов на «Диалогах»
          помечены <span className="ui">↩</span>; эскалированный тред ждёт человека.
        </li>
      </ol>

      <p className="note">
        <b>Пересборка из сырья</b> — когда поменялись веса скоринга или разбор: считает всё заново
        из уже скачанного, в сеть не ходит и денег не стоит. Серые кнопки под пайплайнами — те же
        шаги поштучно (<code className="mono">collect.gis</code>, <code className="mono">probe.serp</code>{" "}
        и прочие), для точечной проверки, а не для обычной работы.
      </p>
    </details>
  );
}

const STATUS: Record<string, string> = {
  queued: "в очереди",
  done: "готово",
  failed: "не прошёл",
  cancelled: "прервано",
};

function totalNumbers(sender: { numbers: Record<string, number> }) {
  return Object.values(sender.numbers).reduce((sum, count) => sum + count, 0);
}
