"use client";

/** Навигация по трём системам. */

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
} from "@phosphor-icons/react";
import { useLive } from "./live";

const NAV = [
  { href: "/", label: "Сегодня", icon: SquaresFourIcon },
  { href: "/leads", label: "Лиды", icon: TargetIcon },
  { href: "/cold", label: "Холодные", icon: PaperPlaneTiltIcon },
  { href: "/threads", label: "Диалоги", icon: PenNibIcon },
  { href: "/analytics", label: "Аналитика", icon: ChartLineUpIcon },
  { href: "/sender", label: "Отправка", icon: BroadcastIcon },
  { href: "/activity", label: "Процессы", icon: PulseIcon },
  { href: "/sandbox", label: "Песочница", icon: FlaskIcon },
];

export default function Sidebar() {
  const pathname = usePathname();
  const { active, connected, stats } = useLive();
  // «Песочница» живёт только при SANDBOX=1: иначе страница встречала бы
  // оператора баннером «GET /api/sandbox/runs — 404» над пустым экраном.
  const nav = NAV.filter((item) => item.href !== "/sandbox" || stats?.sandbox);
  // Задачи считаются из снапшота, а не запросом: он и так приезжает по SSE на
  // каждое событие, и второй источник числа разошёлся бы с первым.
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
            {href !== "/" && active && kindOf(href) === systemOf(active.kind) && (
              <span className="nav-live" title={`${active.title}: ${active.status === "running" ? "выполняется" : "в очереди"}`} />
            )}
          </Link>
        ))}
      </nav>

      <div className="sidebar-foot">
        <span className={`live-dot${connected ? " is-on" : ""}`} />
        {connected ? "данные в реальном времени" : "нет соединения"}
      </div>
    </aside>
  );
}

function kindOf(href: string) {
  if (href === "/leads") return "sourcing";
  if (href === "/cold") return "cold";
  if (href === "/threads") return "writer";
  return "";
}

function systemOf(kind: string) {
  if (kind === "write") return "cold";
  if (kind === "discover" || kind === "classify" || kind === "rebuild") return "sourcing";
  return "";
}
