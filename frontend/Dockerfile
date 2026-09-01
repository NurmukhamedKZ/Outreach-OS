FROM node:22-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# rewrites() запекается в routes-manifest на этапе build, поэтому адрес API
# нужен сборщику, а не рантайму (next start его уже не читает).
ARG API_ORIGIN=http://127.0.0.1:8787
ENV API_ORIGIN=${API_ORIGIN}
# SSE идёт в бэкенд напрямую из браузера (см. CLAUDE.md), а не через rewrites,
# поэтому адрес должен быть тем, что реально резолвится с хоста, а не именем
# сервиса compose — запекается в клиентский бандл на этом же шаге build.
ARG NEXT_PUBLIC_API_ORIGIN=http://localhost:8787
ENV NEXT_PUBLIC_API_ORIGIN=${NEXT_PUBLIC_API_ORIGIN}
RUN npm run build

FROM node:22-slim
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app ./

EXPOSE 3000

CMD ["npm", "start"]
