import type { Metadata } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import "./globals.css";
import { LiveProvider } from "@/components/live";
import Sidebar from "@/components/Sidebar";

const inter = Inter({
  subsets: ["latin", "cyrillic"],
  weight: ["400", "500", "600"],
  variable: "--font-sans",
});

const mono = JetBrains_Mono({
  subsets: ["latin", "cyrillic"],
  weight: ["400", "500"],
  variable: "--font-jetbrains",
});

export const metadata: Metadata = {
  title: "Outreach OS",
  description: "Лиды, персонализация и отправка: три системы, живые процессы",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="ru" className={`${inter.variable} ${mono.variable}`}>
      <body>
        <LiveProvider>
          <div className="shell">
            <Sidebar />
            <main className="content">{children}</main>
          </div>
        </LiveProvider>
      </body>
    </html>
  );
}
