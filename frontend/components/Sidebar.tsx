"use client";

/** Навигация по трём системам. Система 3 закрыта до релиза: пункт виден,
 * замок и бейдж «скоро» честно говорят, что за ней, а клик никуда не ведёт.
 */

import { usePathname } from "next/navigation";
import Link from "next/link";
import {
  LockSimpleIcon,
  PulseIcon,
  TargetIcon,
  PenNibIcon,
  PaperPlaneTiltIcon,
  SquaresFourIcon,
} from "@phosphor-icons/react";
import { useLive } from "./live";

const NAV = [
  { href: "/", label: "Обзор", icon: SquaresFourIcon },
  { href: "/sourcing", label: "Сбор лидов", icon: TargetIcon },
  { href: "/writer", label: "Персонализация", icon: PenNibIcon },
  { href: "/sender", label: "Отправка", icon: PaperPlaneTiltIcon, locked: true },
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
        {NAV.map(({ href, label, icon: Icon, locked }) =>
          locked ? (
            <span key={href} className="nav-item is-locked" aria-disabled="true">
              <Icon size={17} />
              <span className="nav-label">{label}</span>
              <span className="nav-badge">
                <LockSimpleIcon size={11} /> скоро
              </span>
            </span>
          ) : (
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
          ),
        )}
      </nav>

      <div className="sidebar-foot">
        <span className={`live-dot${connected ? " is-on" : ""}`} />
        {connected ? "данные в реальном времени" : "нет соединения"}
      </div>
    </aside>
  );
}

function kindOf(href: string) {
  if (href === "/sourcing") return "sourcing";
  if (href === "/writer") return "writer";
  return "";
}

function systemOf(kind: string) {
  if (kind === "write") return "writer";
  if (kind === "discover" || kind === "classify" || kind === "rebuild") return "sourcing";
  return "";
}
