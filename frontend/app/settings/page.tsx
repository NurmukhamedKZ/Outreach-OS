"use client";

/** Настройки оператора (ADR 0003). Каждый раздел принадлежит своему модулю и
 * пишется руками под него: «Сегменты» модуля companies, «Исследование»
 * модуля research, «Переписка» модуля conversations и «Отправка» модуля
 * delivery. Страница
 * опрашивает API, пока идёт сбор, и не использует SSE (#4, решение 14).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  AUTOPILOT_LABELS,
  SIGNAL_LABELS,
  fetchConversationSettings,
  fetchDeliverySettings,
  saveDeliverySettings,
  fetchResearchSettings,
  saveConversationSettings,
  fetchSegments,
  saveResearchSettings,
  saveSegments,
  type AutopilotMode,
  type ConversationSettings,
  type DeliverySettings,
  type SendWindow,
  type ResearchSettings,
  type SignalType,
  type SegmentSettings,
  type SegmentStatus,
  type SegmentsView,
} from "../api";

const POLL_MS = 3000;

const STATUS_LABEL: Record<SegmentStatus, string> = {
  new: "не собран",
  collecting: "собирается…",
  collected: "собран",
  failed: "ошибка",
};

const ERROR_TEXT: Record<string, string> = {
  "errors.cityInvalid": "Такого города нет среди городов 2GIS.",
  "errors.rubricInvalid": "Id рубрики 2GIS — только цифры, например 653.",
};

const WHEN = new Intl.DateTimeFormat("ru", {
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
});

export default function Settings() {
  return (
    <>
      <header className="page-head">
        <h1>Настройки</h1>
        <span className="page-sub">
          где искать компании, как их исследовать, что им писать и как отправлять
        </span>
      </header>
      <Segments />
      <Research />
      <Conversations />
      <Delivery />
    </>
  );
}

function Segments() {
  const [view, setView] = useState<SegmentsView | null>(null);
  const [draft, setDraft] = useState<SegmentSettings | null>(null);
  // Свои рубрики остаются в списке и после снятия галочки — до перезагрузки.
  const [extra, setExtra] = useState<string[]>([]);
  const [custom, setCustom] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  // Ответ опроса, ушедшего до сохранения, старше ответа самого сохранения.
  const saves = useRef(0);

  const adopt = useCallback((next: SegmentsView) => {
    setView(next);
    setDraft(next.settings);
    const known = new Set(next.rubrics.map((r) => r.id));
    setExtra((old) => [...new Set([...old, ...next.settings.rubrics])].filter((id) => !known.has(id)));
  }, []);

  useEffect(() => {
    fetchSegments().then(adopt).catch((failure) => setError(messageOf(failure)));
  }, [adopt]);

  const collecting = view?.segments.some((s) => s.status === "collecting") ?? false;

  // Опрос обновляет только статусы: правки оператора в черновике не трогает.
  useEffect(() => {
    if (!collecting) return;
    const timer = setInterval(() => {
      const before = saves.current;
      fetchSegments()
        .then((next) => {
          if (saves.current === before) setView(next);
        })
        .catch((failure) => setError(messageOf(failure)));
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [collecting]);

  if (!view || !draft) {
    return <section className="card">{error ? <p className="error-line">{error}</p> : "Загрузка…"}</section>;
  }

  const toggle = (field: keyof SegmentSettings, id: string) => {
    setSaved(false);
    setDraft((old) => old && {
      ...old,
      [field]: old[field].includes(id) ? old[field].filter((x) => x !== id) : [...old[field], id],
    });
  };

  const addCustom = () => {
    const id = custom.trim();
    if (!id) return;
    setExtra((old) => (old.includes(id) || view.rubrics.some((r) => r.id === id) ? old : [...old, id]));
    if (!draft.rubrics.includes(id)) toggle("rubrics", id);
    setCustom("");
  };

  const save = () => {
    setError(null);
    setSaving(true);
    saves.current += 1;
    saveSegments(draft)
      .then((next) => {
        adopt(next);
        setSaved(true);
      })
      .catch((failure) => {
        const code = failure instanceof ApiError ? failure.code : undefined;
        setError((code && ERROR_TEXT[code]) || messageOf(failure));
      })
      .finally(() => setSaving(false));
  };

  const dirty = JSON.stringify(draft) !== JSON.stringify(view.settings);
  const cityName = (id: string) => view.cities.find((c) => c.id === id)?.name ?? id;
  const rubricName = (id: string) => view.rubrics.find((r) => r.id === id)?.name ?? `Рубрика ${id}`;
  const rubrics = [...view.rubrics, ...extra.map((id) => ({ id, name: `Рубрика ${id}` }))];
  // Сохранённый город, которого нет в справочнике, должен сниматься галочкой.
  const cities = [
    ...view.cities,
    ...draft.cities.filter((id) => !view.cities.some((c) => c.id === id)).map((id) => ({ id, name: id })),
  ];

  return (
    <section className="card settings-section" aria-labelledby="segments-title">
      <h2 id="segments-title">Сегменты</h2>
      <p className="card-sub">
        Сегмент — город плюс рубрика 2GIS: каждый выбранный город ищется в каждой
        выбранной рубрике. 2GIS отдаёт ограниченное число организаций на
        рубрику города — у сегмента, упёршегося в потолок, это помечено.
      </p>

      <h3>Города</h3>
      <div className="settings-checks">
        {cities.map((city) => (
          <label key={city.id} className="settings-check">
            <input
              type="checkbox"
              checked={draft.cities.includes(city.id)}
              onChange={() => toggle("cities", city.id)}
            />
            {city.name}
          </label>
        ))}
      </div>

      <h3>Рубрики</h3>
      <div className="settings-checks settings-rubrics">
        {rubrics.map((rubric) => (
          <label key={rubric.id} className="settings-check">
            <input
              type="checkbox"
              checked={draft.rubrics.includes(rubric.id)}
              onChange={() => toggle("rubrics", rubric.id)}
            />
            <span>
              {rubric.name} <span className="settings-id mono">{rubric.id}</span>
            </span>
          </label>
        ))}
      </div>

      <form
        className="settings-custom"
        onSubmit={(event) => {
          event.preventDefault();
          addCustom();
        }}
      >
        <input
          className="input mono"
          aria-label="Id рубрики 2GIS"
          placeholder="id рубрики 2GIS, например 653"
          value={custom}
          onChange={(event) => setCustom(event.target.value)}
        />
        <button type="submit" className="btn-secondary" disabled={!custom.trim()}>
          Добавить рубрику
        </button>
      </form>

      <div className="settings-save">
        <button className="btn" onClick={save} disabled={!dirty || saving}>
          {saving ? "Сохраняю…" : "Сохранить"}
        </button>
        {saved && !dirty && <span className="page-sub">Сохранено</span>}
      </div>
      {error && <p className="error-line" role="alert">{error}</p>}

      <h3>Сбор</h3>
      {view.segments.length === 0 ? (
        <p className="page-sub">Не выбран ни один сегмент.</p>
      ) : (
        <div className="settings-table">
          <table className="table">
            <thead>
              <tr>
                <th>Сегмент</th><th>Статус</th><th>Филиалов</th><th>Компаний</th><th>Обновлён</th>
              </tr>
            </thead>
            <tbody>
              {view.segments.map((segment) => (
                <tr key={`${segment.city}/${segment.rubric}`}>
                  <td>
                    {cityName(segment.city)} · {rubricName(segment.rubric)}
                    {segment.error && <div className="error-line">{segment.error}</div>}
                  </td>
                  <td>
                    {STATUS_LABEL[segment.status]}
                    {segment.capped && <div className="page-sub">потолок 2GIS</div>}
                  </td>
                  <td>{segment.branches}</td>
                  <td>{segment.companies}</td>
                  <td>{segment.updated_at ? WHEN.format(new Date(segment.updated_at)) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Research() {
  const [saved, setSaved] = useState<ResearchSettings | null>(null);
  const [draft, setDraft] = useState<ResearchSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [done, setDone] = useState(false);

  useEffect(() => {
    fetchResearchSettings()
      .then((next) => {
        setSaved(next);
        setDraft(next);
      })
      .catch((failure) => setError(messageOf(failure)));
  }, []);

  if (!saved || !draft) {
    return <section className="card">{error ? <p className="error-line">{error}</p> : "Загрузка…"}</section>;
  }

  const edit = (patch: Partial<ResearchSettings>) => {
    setDone(false);
    setDraft({ ...draft, ...patch });
  };

  const save = () => {
    setError(null);
    setSaving(true);
    saveResearchSettings(draft)
      .then((next) => {
        setSaved(next);
        setDraft(next);
        setDone(true);
      })
      .catch((failure) => {
        // 422 несёт список полей, а не код: границы видны у самих полей.
        const invalid = failure instanceof ApiError && failure.status === 422;
        setError(invalid ? "Значение вне допустимых границ — поправьте поле." : messageOf(failure));
      })
      .finally(() => setSaving(false));
  };

  const dirty = JSON.stringify(draft) !== JSON.stringify(saved);
  const complete = [draft.batch_size, draft.half_life_days, ...Object.values(draft.weights)]
    .every(Number.isFinite);

  return (
    <section className="card settings-section" aria-labelledby="research-title">
      <h2 id="research-title">Исследование</h2>
      <p className="card-sub">
        Партия берёт компании выбранных сегментов по приоритету, читает отзывы и сайт и одним
        вызовом модели пишет досье. Оценка лида — сумма весов его сигналов; вклад сигнала
        вдвое меньше за каждый период полураспада.
      </p>

      {/* key: после сохранения поля перечитывают нормализованные значения. */}
      <form
        key={JSON.stringify(saved)}
        className="settings-fields"
        onSubmit={(event) => {
          event.preventDefault();
          save();
        }}
      >
        <label className="settings-field">
          <span>Размер партии</span>
          <NumberInput
            min={1}
            max={200}
            step={1}
            value={draft.batch_size}
            onChange={(batch_size) => edit({ batch_size })}
          />
          <span className="settings-id">компаний, 1–200</span>
        </label>
        <label className="settings-field">
          <span>Модель</span>
          <input
            className="input mono"
            required
            maxLength={200}
            value={draft.model}
            onChange={(event) => edit({ model: event.target.value })}
          />
          <span className="settings-id">id модели OpenRouter</span>
        </label>
        <label className="settings-field">
          <span>Период полураспада</span>
          <NumberInput
            min={0.1}
            max={3650}
            value={draft.half_life_days}
            onChange={(half_life_days) => edit({ half_life_days })}
          />
          <span className="settings-id">дней</span>
        </label>

        <h3>Веса сигналов</h3>
        {(Object.keys(SIGNAL_LABELS) as SignalType[]).map((signal) => (
          <label key={signal} className="settings-field">
            <span>{SIGNAL_LABELS[signal]}</span>
            <NumberInput
              min={0}
              max={100}
              value={draft.weights[signal]}
              onChange={(weight) => edit({ weights: { ...draft.weights, [signal]: weight } })}
            />
            <span className="settings-id">0–100</span>
          </label>
        ))}

        <div className="settings-save">
          <button type="submit" className="btn" disabled={!dirty || !complete || saving}>
            {saving ? "Сохраняю…" : "Сохранить"}
          </button>
          {done && !dirty && <span className="page-sub">Сохранено</span>}
        </div>
      </form>
      {error && <p className="error-line" role="alert">{error}</p>}
    </section>
  );
}

/** Раздел модуля conversations: модель и размер «Черновики топ-N», о каких
 *  сигналах писать, отсечка давности повода, оффер и его варианты. Сохраняется
 *  целиком, как «Исследование». */
function Conversations() {
  const [saved, setSaved] = useState<ConversationSettings | null>(null);
  const [draft, setDraft] = useState<ConversationSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [done, setDone] = useState(false);

  useEffect(() => {
    fetchConversationSettings()
      .then((next) => {
        setSaved(next);
        setDraft(next);
      })
      .catch((failure) => setError(messageOf(failure)));
  }, []);

  if (!saved || !draft) {
    return <section className="card">{error ? <p className="error-line">{error}</p> : "Загрузка…"}</section>;
  }

  const edit = (patch: Partial<ConversationSettings>) => {
    setDone(false);
    setDraft({ ...draft, ...patch });
  };

  const toggle = (signal: SignalType) =>
    edit({
      pitchable: draft.pitchable.includes(signal)
        ? draft.pitchable.filter((s) => s !== signal)
        : [...draft.pitchable, signal],
    });

  const editVariant = (index: number, patch: Partial<{ id: string; text: string }>) =>
    edit({ variants: draft.variants.map((v, i) => (i === index ? { ...v, ...patch } : v)) });

  const save = () => {
    setError(null);
    setSaving(true);
    saveConversationSettings(draft)
      .then((next) => {
        setSaved(next);
        setDraft(next);
        setDone(true);
      })
      .catch((failure) => {
        const invalid = failure instanceof ApiError && failure.status === 422;
        setError(
          invalid
            ? "Значение вне допустимых границ — поправьте поле. У вариантов id — латиница," +
                " цифры и «_», без повторов."
            : messageOf(failure),
        );
      })
      .finally(() => setSaving(false));
  };

  const dirty = JSON.stringify(draft) !== JSON.stringify(saved);
  const complete =
    [draft.top_n, draft.max_age_days].every(Number.isFinite) &&
    draft.model.trim() !== "" &&
    draft.offer.trim() !== "" &&
    draft.variants.every((v) => v.id.trim() !== "" && v.text.trim() !== "");

  return (
    <section className="card settings-section" aria-labelledby="conversations-title">
      <h2 id="conversations-title">Переписка</h2>
      <p className="card-sub">
        «Черновики топ-N» берёт новых лидов по оценке и пишет каждому первое письмо. Повод —
        зацепка досье или сигнал из списка ниже, не старше отсечки. Тред аргументирует один
        вариант оффера навсегда; без вариантов — базовый оффер.
      </p>

      <form
        key={JSON.stringify(saved)}
        className="settings-fields"
        onSubmit={(event) => {
          event.preventDefault();
          save();
        }}
      >
        <label className="settings-field">
          <span>Лидов за прогон</span>
          <NumberInput
            min={1}
            max={100}
            step={1}
            value={draft.top_n}
            onChange={(top_n) => edit({ top_n })}
          />
          <span className="settings-id">«Черновики топ-N», 1–100</span>
        </label>
        <label className="settings-field">
          <span>Модель</span>
          <input
            className="input mono"
            required
            maxLength={200}
            value={draft.model}
            onChange={(event) => edit({ model: event.target.value })}
          />
          <span className="settings-id">id модели OpenRouter</span>
        </label>
        <label className="settings-field">
          <span>Повод не старше</span>
          <NumberInput
            min={1}
            max={3650}
            step={1}
            value={draft.max_age_days}
            onChange={(max_age_days) => edit({ max_age_days })}
          />
          <span className="settings-id">дней</span>
        </label>

        <h3>О каких сигналах писать</h3>
        <div className="settings-checks settings-rubrics">
          {(Object.keys(SIGNAL_LABELS) as SignalType[]).map((signal) => (
            <label key={signal} className="settings-check">
              <input
                type="checkbox"
                checked={draft.pitchable.includes(signal)}
                onChange={() => toggle(signal)}
              />
              {SIGNAL_LABELS[signal]}
            </label>
          ))}
        </div>

        <h3>Оффер</h3>
        <textarea
          className="settings-text"
          required
          maxLength={2000}
          rows={5}
          aria-label="Оффер"
          value={draft.offer}
          onChange={(event) => edit({ offer: event.target.value })}
        />

        <h3>Варианты оффера</h3>
        {draft.variants.map((variant, index) => (
          <div key={index} className="settings-variant">
            <div className="settings-variant-head">
              <input
                className="input mono"
                required
                maxLength={64}
                pattern="[a-z0-9_]+"
                aria-label={`id варианта ${index + 1}`}
                value={variant.id}
                onChange={(event) => editVariant(index, { id: event.target.value })}
              />
              <button
                type="button"
                className="btn-secondary"
                onClick={() => edit({ variants: draft.variants.filter((_, i) => i !== index) })}
              >
                Убрать
              </button>
            </div>
            <textarea
              className="settings-text"
              required
              maxLength={2000}
              rows={3}
              aria-label={`Текст варианта ${index + 1}`}
              value={variant.text}
              onChange={(event) => editVariant(index, { text: event.target.value })}
            />
          </div>
        ))}
        <div>
          <button
            type="button"
            className="btn-secondary"
            disabled={draft.variants.length >= 10}
            onClick={() => edit({ variants: [...draft.variants, { id: "", text: "" }] })}
          >
            Добавить вариант
          </button>
        </div>

        <div className="settings-save">
          <button type="submit" className="btn" disabled={!dirty || !complete || saving}>
            {saving ? "Сохраняю…" : "Сохранить"}
          </button>
          {done && !dirty && <span className="page-sub">Сохранено</span>}
        </div>
      </form>
      {error && <p className="error-line" role="alert">{error}</p>}
    </section>
  );
}

const WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

/** Раздел модуля delivery: режим автопилота, окна, дневной потолок, каденция,
 *  стоп-слова и предохранители автоответа. Сохраняется целиком; режим — та же
 *  настройка, что переключатель на «Отправке». */
function Delivery() {
  const [saved, setSaved] = useState<DeliverySettings | null>(null);
  const [draft, setDraft] = useState<DeliverySettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [done, setDone] = useState(false);

  useEffect(() => {
    fetchDeliverySettings()
      .then((next) => {
        setSaved(next);
        setDraft(next);
      })
      .catch((failure) => setError(messageOf(failure)));
  }, []);

  if (!saved || !draft) {
    return <section className="card">{error ? <p className="error-line">{error}</p> : "Загрузка…"}</section>;
  }

  const edit = (patch: Partial<DeliverySettings>) => {
    setDone(false);
    setDraft({ ...draft, ...patch });
  };

  const save = () => {
    setError(null);
    setSaving(true);
    // Режим, которого оператор в форме не трогал, берётся живым: иначе форма, открытая до
    // аварийного «Стоп» на «Отправке», вернула бы старый режим и снова открыла очередь.
    fetchDeliverySettings()
      .then((live) =>
        saveDeliverySettings({
          ...draft,
          autopilot: draft.autopilot === saved.autopilot ? live.autopilot : draft.autopilot,
        }),
      )
      .then((next) => {
        setSaved(next);
        setDraft(next);
        setDone(true);
      })
      .catch((failure) => {
        const invalid = failure instanceof ApiError && failure.status === 422;
        setError(
          invalid
            ? "Значение не подходит: окно открывается раньше, чем закрывается, и в нём есть день;" +
                " паузы — дни от 1 до 60; стоп-слово — от двух букв."
            : messageOf(failure),
        );
      })
      .finally(() => setSaving(false));
  };

  const dirty = JSON.stringify(draft) !== JSON.stringify(saved);
  const complete =
    [draft.daily_ceiling, draft.auto_replies_per_thread, draft.max_reply_chars,
      ...draft.window.hours, ...draft.reply_window.hours, ...draft.follow_up_days]
      .every(Number.isFinite) &&
    draft.follow_up_days.length > 0 &&
    draft.stopwords.length > 0;

  return (
    <section className="card settings-section" aria-labelledby="delivery-title">
      <h2 id="delivery-title">Отправка</h2>
      <p className="card-sub">
        Режим решает, что уходит само: «Стоп» держит всю очередь, «Ручной» — только поставленное
        вами, «Только ответы» сам отвечает лидам, «Полный автопилот» — ещё и первые письма и
        касания. Окна — по времени Алматы. Темп, джиттер, прогрев и ретраи — в коде: они
        защищают номер от бана.
      </p>

      <form
        key={JSON.stringify(saved)}
        className="settings-fields"
        onSubmit={(event) => {
          event.preventDefault();
          save();
        }}
      >
        <label className="settings-field">
          <span>Режим автопилота</span>
          <select
            className="input"
            value={draft.autopilot}
            onChange={(event) => edit({ autopilot: event.target.value as AutopilotMode })}
          >
            {(Object.entries(AUTOPILOT_LABELS) as [AutopilotMode, string][]).map(([mode, label]) => (
              <option key={mode} value={mode}>{label}</option>
            ))}
          </select>
          <span className="settings-id">то же, что на «Отправке»</span>
        </label>

        <h3>Окно отправки</h3>
        <WindowFields
          name="Окно отправки"
          value={draft.window}
          onChange={(window) => edit({ window })}
        />
        <p className="page-sub">Первые письма и касания: будят человека, поэтому в рабочее время.</p>

        <h3>Окно ответа</h3>
        <WindowFields
          name="Окно ответа"
          value={draft.reply_window}
          onChange={(reply_window) => edit({ reply_window })}
        />
        <p className="page-sub">Ответ лиду, который написал сам и ждёт сейчас.</p>

        <h3>Лимиты и каденция</h3>
        <label className="settings-field">
          <span>Потолок в день на номер</span>
          <NumberInput
            min={1}
            max={30}
            step={1}
            value={draft.daily_ceiling}
            onChange={(daily_ceiling) => edit({ daily_ceiling })}
          />
          <span className="settings-id">сообщений, 1–30: выше растёт риск бана</span>
        </label>
        <label className="settings-field">
          <span>Паузы между касаниями</span>
          <DaysInput value={draft.follow_up_days} onChange={(follow_up_days) => edit({ follow_up_days })} />
          <span className="settings-id">дней через запятую · касаний: {saved.touches}</span>
        </label>
        <label className="settings-field">
          <span>Автоответов на тред</span>
          <NumberInput
            min={0}
            max={10}
            step={1}
            value={draft.auto_replies_per_thread}
            onChange={(auto_replies_per_thread) => edit({ auto_replies_per_thread })}
          />
          <span className="settings-id">в «Только ответы» и «Полном»; дальше — к человеку</span>
        </label>
        <label className="settings-field">
          <span>Длина автоответа</span>
          <NumberInput
            min={50}
            max={4000}
            step={1}
            value={draft.max_reply_chars}
            onChange={(max_reply_chars) => edit({ max_reply_chars })}
          />
          <span className="settings-id">знаков; длиннее или с ценой — к человеку</span>
        </label>

        <h3>Стоп-слова</h3>
        <textarea
          className="settings-text"
          rows={6}
          aria-label="Стоп-слова"
          value={draft.stopwords.join("\n")}
          onChange={(event) =>
            edit({ stopwords: event.target.value.split("\n").map((line) => line.trim()).filter(Boolean) })
          }
        />
        <p className="page-sub">
          По одному на строку. Фрагмент текста, а не слово целиком: «отпиш» закрывает и «отпишите»,
          и «отпишитесь». Сработавшее стоп-слово закрывает тред и записывает отказ — до модели.
        </p>

        <div className="settings-save">
          <button type="submit" className="btn" disabled={!dirty || !complete || saving}>
            {saving ? "Сохраняю…" : "Сохранить"}
          </button>
          {done && !dirty && <span className="page-sub">Сохранено</span>}
        </div>
      </form>
      {error && <p className="error-line" role="alert">{error}</p>}
    </section>
  );
}

function WindowFields({
  name,
  value,
  onChange,
}: {
  name: string;
  value: SendWindow;
  onChange: (value: SendWindow) => void;
}) {
  const [opens, closes] = value.hours;
  const toggle = (day: number) =>
    onChange({
      ...value,
      weekdays: value.weekdays.includes(day)
        ? value.weekdays.filter((d) => d !== day)
        : [...value.weekdays, day].sort(),
    });
  return (
    <>
      <label className="settings-field">
        <span>Открывается</span>
        <NumberInput min={0} max={23} step={1} value={opens} onChange={(hour) => onChange({ ...value, hours: [hour, closes] })} />
        <span className="settings-id">час, 0–23</span>
      </label>
      <label className="settings-field">
        <span>Закрывается</span>
        <NumberInput min={1} max={24} step={1} value={closes} onChange={(hour) => onChange({ ...value, hours: [opens, hour] })} />
        <span className="settings-id">час, 1–24, не включая</span>
      </label>
      <div className="settings-days" role="group" aria-label={`${name}: дни`}>
        {WEEKDAYS.map((label, index) => (
          <label key={label} className="settings-check">
            <input
              type="checkbox"
              checked={value.weekdays.includes(index + 1)}
              onChange={() => toggle(index + 1)}
            />
            {label}
          </label>
        ))}
      </div>
    </>
  );
}

/** Дни через запятую: «3, 7». Кривое число уходит наверх как NaN, и «Сохранить» ждёт. */
function DaysInput({ value, onChange }: { value: number[]; onChange: (value: number[]) => void }) {
  const [text, setText] = useState(value.join(", "));
  return (
    <input
      className="input mono"
      required
      inputMode="numeric"
      value={text}
      onChange={(event) => {
        setText(event.target.value);
        const parts = event.target.value.split(",").map((part) => part.trim()).filter(Boolean);
        onChange(parts.map((part) => (/^\d+$/.test(part) ? Number(part) : NaN)));
      }}
    />
  );
}

/** Число со своим текстом: недописанное «0.» или пустое поле не перезаписывается
 *  числом из черновика; наверх уходит NaN, и «Сохранить» ждёт числа. */
function NumberInput({
  value,
  onChange,
  min,
  max,
  step = "any",
}: {
  value: number;
  onChange: (value: number) => void;
  min: number;
  max: number;
  step?: number | "any";
}) {
  const [text, setText] = useState(String(value));
  return (
    <input
      className="input mono"
      type="number"
      min={min}
      max={max}
      step={step}
      required
      value={text}
      onChange={(event) => {
        setText(event.target.value);
        onChange(event.target.value === "" ? NaN : event.target.valueAsNumber);
      }}
    />
  );
}

function messageOf(failure: unknown) {
  return failure instanceof Error ? failure.message : String(failure);
}
