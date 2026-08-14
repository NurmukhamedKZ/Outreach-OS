import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Лиды — Казахстан",
  description: "B2B-компании с рабочим каналом и обоснованием why_now",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="ru">
      <body>{children}</body>
    </html>
  );
}
