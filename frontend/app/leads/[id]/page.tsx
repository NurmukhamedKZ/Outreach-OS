"use client";

/** Карточка лида: всё, что система о нём знает. После отказа возвращаем на
 *  /leads: лид мог исчезнуть из выдачи, и оставлять оператора на карточке,
 *  которой нет в списке, — врать ему.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useParams } from "next/navigation";
import { fetchLead, type LeadDetail } from "../../api";
import LeadDossier from "@/components/LeadDossier";

export default function LeadPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const [lead, setLead] = useState<LeadDetail | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  // useParams отдаёт сегмент как есть — в этом Next уже закодированная форма
  // (`dom%3A…`), и двойное кодирование дало бы 404 на ровном месте. Один
  // decodeURIComponent безвреден для раскодированного значения.
  const id = decodeURIComponent(params.id);

  useEffect(() => {
    let stale = false;
    fetchLead(id)
      .then((data) => !stale && setLead(data))
      .catch((error: Error) => !stale && setFailure(error.message));
    return () => {
      stale = true;
    };
  }, [id]);

  if (failure) {
    return (
      <div className="failure">
        <b>Карточка не открылась.</b> {failure}
        <br />
        <Link href="/leads">← к выдаче</Link>
      </div>
    );
  }

  if (!lead) return <p className="placeholder">Загрузка…</p>;

  return (
    <article>
      <Link href="/leads" className="ghost">
        ← к выдаче
      </Link>
      <div style={{ marginTop: 12 }}>
        <LeadDossier lead={lead} onRefused={() => router.push("/leads")} />
      </div>
    </article>
  );
}
