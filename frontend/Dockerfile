FROM node:22-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# rewrites() запекается в routes-manifest на этапе build, поэтому адрес API
# нужен сборщику, а не рантайму (next start его уже не читает).
ARG API_ORIGIN=http://127.0.0.1:8787
ENV API_ORIGIN=${API_ORIGIN}
RUN npm run build

FROM node:22-slim
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app ./

EXPOSE 3000

CMD ["npm", "start"]
