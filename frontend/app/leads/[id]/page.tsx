"use client";

/** Карточка компании из research. После отказа возвращаем на /leads: лид
 *  исчез из выдачи, и оставлять оператора на карточке, которой нет в списке, —
 *  врать ему. «Исследовать заново» — партия из одной компании; карточка
 *  перечитывается, когда партия закончилась.
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useParams } from "next/navigation";
import {
  fetchCompanyResearch,
  researchAgain,
  researchError,
  type CompanyResearch,
} from "../../api";
import LeadDossier from "@/components/LeadDossier";
import { isRunning, useBatch } from "@/components/BatchPanel";

export default function LeadPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const [card, setCard] = useState<CompanyResearch | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // useParams отдаёт сегмент как есть — в этом Next уже закодированная форма,
  // и двойное кодирование дало бы 404. Один decodeURIComponent безвреден.
  const id = decodeURIComponent(params.id);

  const load = useCallback(() => {
    fetchCompanyResearch(id)
      .then(setCard)
      .catch((failure) => setFailure(researchError(failure)));
  }, [id]);

  useEffect(load, [load]);
  const { batch, adopt } = useBatch(load);
  const running = isRunning(batch);

  if (failure) {
    return (
      <div className="failure">
        <b>Карточка не открылась.</b> {failure}
        <br />
        <Link href="/leads">← к лидам</Link>
      </div>
    );
  }

  if (!card) return <p className="placeholder">Загрузка…</p>;

  const again = () => {
    setBusy(true);
    setError(null);
    researchAgain(card.company_id)
      .then(adopt)
      .catch((failure) => setError(researchError(failure)))
      .finally(() => setBusy(false));
  };
  const mine = running && batch!.companies.some((c) => c.company_id === card.company_id);

  return (
    <article>
      <Link href="/leads" className="ghost">
        ← к лидам
      </Link>
      <div style={{ marginTop: 12 }}>
        <LeadDossier
          card={card}
          onRefused={() => router.push("/leads")}
          actions={
            !card.refused && (
              <>
                <button className="btn-secondary" onClick={again} disabled={busy || running}>
                  {mine ? "Исследуется…" : "Исследовать заново"}
                </button>
                {running && !mine && <span className="page-sub">идёт партия — дождитесь её</span>}
                {error && (
                  <span className="error-line" role="alert">
                    {error}
                  </span>
                )}
              </>
            )
          }
        />
      </div>
    </article>
  );
}
