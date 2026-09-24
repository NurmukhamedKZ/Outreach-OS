import type { NextConfig } from "next";

// Бэкенд проксируется под тем же origin: фронтенду не нужен ни адрес API в
// переменных окружения, ни CORS. Один порт для человека — один для браузера.
const API = process.env.API_ORIGIN ?? "http://127.0.0.1:8787";

const nextConfig: NextConfig = {
  // Страницы, которые разъехались по другим адресам: старые закладки оператора
  // не должны отдавать 404.
  async redirects() {
    return [
      { source: "/sourcing", destination: "/leads", permanent: false },
      { source: "/writer", destination: "/threads", permanent: false },
    ];
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API}/api/:path*` }];
  },
};

export default nextConfig;
