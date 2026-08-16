"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchCommands,
  fetchRunTail,
  startRun,
  stopRun,
  type Command,
  type RunTail,
} from "../api";

const POLL_MS = 800;

export default function Runs() {
  const [commands, setCommands] = useState<Command[]>([]);
  const [lines, setLines] = useState<string[]>([]);
  const [run, setRun] = useState<RunTail | null>(null);
  const [args, setArgs] = useState("");
  const [failure, setFailure] = useState<string | null>(null);
  const offset = useRef(0);
  const log = useRef<HTMLPreElement>(null);

  // Лог интересен последней строкой: она объясняет, почему процесс ещё идёт.
  useEffect(() => {
    log.current?.scrollTo({ top: log.current.scrollHeight });
  }, [lines]);

  const poll = useCallback(async () => {
    const tail = await fetchRunTail(offset.current);
    offset.current = tail.offset;
    if (tail.lines.length) setLines((shown) => [...shown, ...tail.lines]);
    setRun(tail);
  }, []);

  // Опрос идёт всё время, пока открыта страница: запуск, начатый в другой
  // вкладке или до перезагрузки, виден так же, как свой.
  useEffect(() => {
    const complain = (error: Error) => setFailure(error.message);
    fetchCommands().then(setCommands).catch(complain);
    poll().catch(complain);
    const timer = setInterval(() => poll().catch(complain), POLL_MS);
    return () => clearInterval(timer);
  }, [poll]);

  const running = run?.name !== null && run?.code === null;

  async function launch(command: Command) {
    setFailure(null);
    setLines([]);
    offset.current = 0;
    try {
      await startRun(command.name, args);
      await poll();
    } catch (error) {
      setFailure((error as Error).message);
    }
  }

  return (
    <main className="console">
      <header className="masthead">
        <h1>Скрипты</h1>
        <nav className="counters">
          <Link href="/">← к лидам</Link>
        </nav>
        <div className="controls">
          <input
            className="args mono"
            value={args}
            placeholder="аргументы, например --cities almaty"
            onChange={(event) => setArgs(event.target.value)}
          />
          {running && (
            <button className="ghost" onClick={() => stopRun().catch(() => poll())}>
              Прервать
            </button>
          )}
        </div>
      </header>

      {failure && <div className="failure">{failure}</div>}

      <div className="split">
        <div className="roster">
          {commands.map((command) => (
            <button
              key={command.name}
              className="row command"
              aria-current={command.name === run?.name}
              disabled={running}
              onClick={() => launch(command)}
            >
              <span className="row-text">
                <span className="row-name">{command.title}</span>
                <span className="row-sub mono">{command.command}</span>
              </span>
            </button>
          ))}
        </div>

        {lines.length > 0 ? (
          <pre className="log mono" ref={log} aria-busy={running}>
            {lines.join("\n")}
            {running ? "\n…" : `\n— код возврата ${run?.code}`}
          </pre>
        ) : (
          <p className="placeholder">
            Выберите скрипт слева. Порядок конвейера: collect → classify → build → report → check.
          </p>
        )}
      </div>
    </main>
  );
}
