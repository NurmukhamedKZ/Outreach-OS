"use client";

/** Навигация по трём системам. */

import { useCallback, useState } from "react";
import { usePathname } from "next/navigation";
import Link from "next/link";
import {
  PulseIcon,
  TargetIcon,
  PenNibIcon,
  PaperPlaneTiltIcon,
  BroadcastIcon,
  SquaresFourIcon,
  ChartLineUpIcon,
  FlaskIcon,
  GearSixIcon,
} from "@phosphor-icons/react";
import { fetchStats, type DashboardStats } from "@/app/api";
import { usePoll } from "./usePoll";

const POLL_MS = 30_000;

const NAV = [
  { href: "/", label: "Сегодня", icon: SquaresFourIcon },
  { href: "/leads", label: "Лиды", icon: TargetIcon },
  { href: "/cold", label: "Холодные", icon: PaperPlaneTiltIcon },
  { href: "/threads", label: "Диалоги", icon: PenNibIcon },
  { href: "/analytics", label: "Аналитика", icon: ChartLineUpIcon },
  { href: "/sender", label: "Отправка", icon: BroadcastIcon },
  { href: "/activity", label: "Процессы", icon: PulseIcon },
  { href: "/settings", label: "Настройки", icon: GearSixIcon },
  { href: "/sandbox", label: "Песочница", icon: FlaskIcon },
];

export default function Sidebar() {
  const pathname = usePathname();
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [connected, setConnected] = useState(true);
  const load = useCallback(() => {
    fetchStats()
      .then((next) => {
        setStats(next);
        setConnected(true);
      })
      .catch(() => setConnected(false));
  }, []);
  usePoll(load, POLL_MS);
  // «Песочница» живёт только при SANDBOX=1: иначе страница встречала бы
  // оператора баннером «GET /api/sandbox/runs — 404» над пустым экраном.
  const nav = NAV.filter((item) => item.href !== "/sandbox" || stats?.sandbox);
  // Бейдж — из счётчиков /api/stats, тех же, что на «Сегодня»: число не выдумывает страница.
  const waiting = (stats?.sender.threads.escalated ?? 0) + (stats?.writer.drafts ?? 0);

  return (
    <aside className="sidebar">
      <Link href="/" className="brand">
        <span className="brand-mark">
          <PulseIcon size={18} weight="bold" />
        </span>
        <span className="brand-text">
          Outreach OS
          <span className="brand-sub">лидогенерация · Казахстан</span>
        </span>
      </Link>

      <nav className="nav">
        {nav.map(({ href, label, icon: Icon }) => (
          <Link
            key={href}
            href={href}
            className="nav-item"
            aria-current={pathname === href}
          >
            <Icon size={17} />
            <span className="nav-label">{label}</span>
            {(href === "/" || href === "/threads") && waiting > 0 && (
              <span className="nav-badge">{waiting}</span>
            )}
          </Link>
        ))}
      </nav>

      <div className="sidebar-foot">
        <span className={`live-dot${connected ? " is-on" : ""}`} />
        {connected ? "бэкенд на связи" : "нет связи с бэкендом"}
      </div>
    </aside>
  );
}
