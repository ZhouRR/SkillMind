FROM node:26.3.1-alpine AS builder

ARG SKILLMIND_CONTEXT_PATH=/skillmind
ENV SKILLMIND_CONTEXT_PATH=${SKILLMIND_CONTEXT_PATH}

RUN npm install --global pnpm@11.7.0
WORKDIR /build

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .
RUN pnpm build

FROM nginx:1.28-alpine

COPY nginx.conf /etc/nginx/nginx.conf
COPY --from=builder /build/dist /usr/share/nginx/html

USER nginx
EXPOSE 8080
