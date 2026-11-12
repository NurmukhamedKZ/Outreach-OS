"use client";

/** Настройки оператора (ADR 0003). Каждый раздел принадлежит своему модулю и
 * пишется руками под него; первый — «Сегменты» модуля companies. Страница
 * опрашивает API, пока идёт сбор, и не использует SSE (#4, решение 14).
 */

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  fetchSegments,
  saveSegments,
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
        <span className="page-sub">где искать компании и как идёт сбор</span>
      </header>
      <Segments />
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

  const adopt = useCallback((next: SegmentsView) => {
    setView(next);
    setDraft(next.settings);
    const known = new Set(next.rubrics.map((r) => r.id));
    setExtra((old) => [...new Set([...old, ...next.settings.rubrics])].filter((id) => !known.has(id)));
  }, []);

  useEffect(() => {
    fetchSegments().then(adopt).catch((failure) => setError(String(failure)));
  }, [adopt]);

  const collecting = view?.segments.some((s) => s.status === "collecting") ?? false;

  // Опрос обновляет только статусы: правки оператора в черновике не трогает.
  useEffect(() => {
    if (!collecting) return;
    const timer = setInterval(() => {
      fetchSegments().then(setView).catch(() => undefined);
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
    saveSegments(draft)
      .then((next) => {
        adopt(next);
        setSaved(true);
      })
      .catch((failure) => {
        const code = failure instanceof ApiError ? failure.code : undefined;
        setError((code && ERROR_TEXT[code]) ?? String(failure));
      })
      .finally(() => setSaving(false));
  };

  const dirty = JSON.stringify(draft) !== JSON.stringify(view.settings);
  const cityName = (id: string) => view.cities.find((c) => c.id === id)?.name ?? id;
  const rubricName = (id: string) => view.rubrics.find((r) => r.id === id)?.name ?? `Рубрика ${id}`;
  const rubrics = [...view.rubrics, ...extra.map((id) => ({ id, name: `Рубрика ${id}` }))];

  return (
    <section className="card settings-section" aria-labelledby="segments-title">
      <h2 id="segments-title">Сегменты</h2>
      <p className="card-sub">
        Сегмент — город плюс рубрика 2GIS: каждый выбранный город ищется в каждой
        выбранной рубрике. 2GIS отдаёт до 60 организаций на рубрику города.
      </p>

      <h3>Города</h3>
      <div className="settings-checks">
        {view.cities.map((city) => (
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
