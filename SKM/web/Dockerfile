FROM node:26.3.1-alpine AS builder

ARG SKILLMIND_CONTEXT_PATH=/skillmind
ENV SKILLMIND_CONTEXT_PATH=${SKILLMIND_CONTEXT_PATH}

RUN npm install --global pnpm@11.7.0
WORKDIR /build/web

COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

# test の相対 import を保ち、型検査に使う契約は builder だけへ置く。
COPY web/ ./
COPY contracts/ /build/contracts/
RUN pnpm build

FROM nginx:1.28-alpine

ARG SKILLMIND_CONTEXT_PATH=/skillmind
LABEL org.skillmind.context-path=${SKILLMIND_CONTEXT_PATH}

COPY web/nginx.conf /etc/nginx/nginx.conf
COPY --from=builder /build/web/dist /usr/share/nginx/html

USER nginx
EXPOSE 8080
