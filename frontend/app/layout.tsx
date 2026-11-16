import type { Metadata } from "next";
import { Suspense } from "react";
import localFont from "next/font/local";
import "./globals.css";
import Sidebar from "@/components/Sidebar";
import Highlighter from "@/components/Highlighter";

// Files in the repo, not Google Fonts: Turbopack kept Google's answer in its cache
// and failed every build on it (docs/agents/bugs.md, #24). Variable, latin and cyrillic;
// Inter's opsz is pinned at 14, as Google serves it.
const inter = localFont({
  src: "./fonts/Inter.woff2",
  weight: "100 900",
  variable: "--font-sans",
});

const jetbrainsMono = localFont({
  src: "./fonts/JetBrainsMono.woff2",
  weight: "100 800",
  variable: "--font-jetbrains",
});

export const metadata: Metadata = {
  title: "Outreach OS",
  description: "Лиды, персонализация и отправка: три системы",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="ru" className={`${inter.variable} ${jetbrainsMono.variable}`}>
      <body>
        <Suspense fallback={null}>
          <Highlighter />
        </Suspense>
        <div className="shell">
          <Sidebar />
          <main className="content">{children}</main>
        </div>
      </body>
    </html>
  );
}
