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
} from "@phosphor-icons/react";
import { useLive } from "./live";

const NAV = [
  { href: "/", label: "Обзор", icon: SquaresFourIcon },
  { href: "/leads", label: "Лиды", icon: TargetIcon },
  { href: "/cold", label: "Холодные", icon: PaperPlaneTiltIcon },
  { href: "/threads", label: "Диалоги", icon: PenNibIcon },
  { href: "/sender", label: "Отправка", icon: BroadcastIcon },
  { href: "/activity", label: "Процессы", icon: PulseIcon },
];

export default function Sidebar() {
  const pathname = usePathname();
  const { active, connected } = useLive();

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
        {NAV.map(({ href, label, icon: Icon }) => (
          <Link
            key={href}
            href={href}
            className="nav-item"
            aria-current={pathname === href}
          >
            <Icon size={17} />
            <span className="nav-label">{label}</span>
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
