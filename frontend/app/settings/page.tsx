"use client";

/** Настройки оператора (ADR 0003). Каждый раздел принадлежит своему модулю и
 * пишется руками под него: «Сегменты» модуля companies и «Исследование»
 * модуля research. Страница
 * опрашивает API, пока идёт сбор, и не использует SSE (#4, решение 14).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  SIGNAL_LABELS,
  fetchResearchSettings,
  fetchSegments,
  saveResearchSettings,
  saveSegments,
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
        <span className="page-sub">где искать компании, как идёт сбор и как их исследовать</span>
      </header>
      <Segments />
      <Research />
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

  return (
    <section className="card settings-section" aria-labelledby="research-title">
      <h2 id="research-title">Исследование</h2>
      <p className="card-sub">
        Партия берёт компании выбранных сегментов по приоритету, читает отзывы и сайт и одним
        вызовом модели пишет досье. Оценка лида — сумма весов его сигналов; вклад сигнала
        вдвое меньше за каждый период полураспада.
      </p>

      <form
        className="settings-fields"
        onSubmit={(event) => {
          event.preventDefault();
          save();
        }}
      >
        <label className="settings-field">
          <span>Размер партии</span>
          <input
            className="input mono"
            type="number"
            min={1}
            max={200}
            step={1}
            required
            value={draft.batch_size}
            onChange={(event) => edit({ batch_size: event.target.valueAsNumber })}
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
          <input
            className="input mono"
            type="number"
            min={0.1}
            max={3650}
            step="any"
            required
            value={draft.half_life_days}
            onChange={(event) => edit({ half_life_days: event.target.valueAsNumber })}
          />
          <span className="settings-id">дней</span>
        </label>

        <h3>Веса сигналов</h3>
        {(Object.keys(SIGNAL_LABELS) as SignalType[]).map((signal) => (
          <label key={signal} className="settings-field">
            <span>{SIGNAL_LABELS[signal]}</span>
            <input
              className="input mono"
              type="number"
              min={0}
              max={100}
              step="any"
              required
              value={draft.weights[signal]}
              onChange={(event) =>
                edit({ weights: { ...draft.weights, [signal]: event.target.valueAsNumber } })
              }
            />
            <span className="settings-id">0–100</span>
          </label>
        ))}

        <div className="settings-save">
          <button type="submit" className="btn" disabled={!dirty || saving}>
            {saving ? "Сохраняю…" : "Сохранить"}
          </button>
          {done && !dirty && <span className="page-sub">Сохранено</span>}
        </div>
      </form>
      {error && <p className="error-line" role="alert">{error}</p>}
    </section>
  );
}

function messageOf(failure: unknown) {
  return failure instanceof Error ? failure.message : String(failure);
}
