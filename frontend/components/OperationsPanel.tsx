"use client";

import { useState } from "react";
import { startOperation } from "@/app/api";
import { useLive } from "./live";

export function OperationsPanel({ operations }: { operations: string[] }) {
  const { active } = useLive();
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  async function run(name: string) {
    setBusy(name);
    setFailure(null);
    try {
      await startOperation(name);
    } catch (error) {
      setFailure((error as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="pipeline-actions">
      {operations.map((name) => (
        <button key={name} className="btn btn-quiet"
          disabled={!!active || busy === name} onClick={() => run(name)}>
          {busy === name ? "ставится…" : name}
        </button>
      ))}
      {failure && <p className="form-error">{failure}</p>}
    </div>
  );
}