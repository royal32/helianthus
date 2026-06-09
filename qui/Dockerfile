FROM alpine/git:2.49.1 AS source

ARG QUI_REF=v1.20.0

RUN git clone --depth 1 --branch "${QUI_REF}" https://github.com/autobrr/qui.git /src

WORKDIR /src

COPY visible-external-ip.patch /tmp/visible-external-ip.patch
RUN git apply --check /tmp/visible-external-ip.patch && \
    git apply /tmp/visible-external-ip.patch

FROM node:22.18-alpine AS frontend-builder

RUN npm install -g corepack@latest && \
    corepack enable

WORKDIR /app/web

COPY --from=source /src/web/package.json /src/web/pnpm-lock.yaml ./
RUN pnpm config set dangerouslyAllowAllBuilds true && \
    pnpm install --frozen-lockfile

COPY --from=source /src/web ./
RUN pnpm run build

FROM --platform=$BUILDPLATFORM golang:1.26-alpine3.22 AS go-builder

ARG TARGETOS
ARG TARGETARCH
ARG TARGETVARIANT
ARG QUI_REF=v1.20.0

WORKDIR /app

COPY --from=source /src/go.mod /src/go.sum ./
RUN go mod download

COPY --from=source /src ./
COPY --from=frontend-builder /app/web/dist ./web/dist

ENV GOOS=${TARGETOS} \
    GOARCH=${TARGETARCH}

RUN case "${TARGETARCH}" in \
      "arm") \
        case "${TARGETVARIANT}" in \
          "v6") export GOARM=6 ;; \
          "v7") export GOARM=7 ;; \
        esac \
      ;; \
    esac && \
    CGO_ENABLED=0 go build \
      -ldflags "-s -w -X github.com/autobrr/qui/internal/buildinfo.Version=${QUI_REF}" \
      -o qui ./cmd/qui

FROM alpine:3.23

ENV HOME="/config" \
    XDG_CONFIG_HOME="/config" \
    XDG_DATA_HOME="/config"

RUN apk --no-cache add ca-certificates curl tzdata bash

WORKDIR /config

VOLUME /config

COPY --from=go-builder /app/qui /usr/local/bin/

EXPOSE 7476

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:7476/health || exit 1

ENTRYPOINT ["/usr/local/bin/qui"]
CMD ["serve"]
