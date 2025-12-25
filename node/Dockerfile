# Stage 1: System dependencies
FROM python:3.11-slim AS base
RUN apt-get update && apt-get install -y \
    gcc \
    iptables \
    curl \
    unzip \
    gzip \
    ca-certificates \
    wireguard-tools \
    iproute2 \
    && rm -rf /var/lib/apt/lists/*

# Stage 2: Install FRP (cached layer)
FROM base AS frp-bin
RUN set -eux; \
    ARCH_RAW="$(uname -m)"; \
    case "$ARCH_RAW" in \
        x86_64) ARCH="amd64" ;; \
        aarch64) ARCH="arm64" ;; \
        *) ARCH="amd64" ;; \
    esac; \
    echo "Fetching FRP release"; \
    FRP_VERSION="v0.65.0"; \
    FRP_VERSION_NO_V="${FRP_VERSION#v}"; \
    FRP_URL="https://github.com/fatedier/frp/releases/download/${FRP_VERSION}/frp_${FRP_VERSION_NO_V}_linux_${ARCH}.tar.gz"; \
    FRP_TMP="$(mktemp -d)"; \
    curl -fsSL "$FRP_URL" -o "$FRP_TMP/frp.tar.gz"; \
    tar -xzf "$FRP_TMP/frp.tar.gz" -C "$FRP_TMP"; \
    FRPC_BIN="$(find "$FRP_TMP" -type f -name 'frpc' | head -n1)"; \
    if [ -n "$FRPC_BIN" ]; then \
        install -Dm755 "$FRPC_BIN" /usr/local/bin/frpc; \
    fi; \
    FRPS_BIN="$(find "$FRP_TMP" -type f -name 'frps' | head -n1)"; \
    if [ -n "$FRPS_BIN" ]; then \
        install -Dm755 "$FRPS_BIN" /usr/local/bin/frps; \
    fi; \
    rm -rf "$FRP_TMP"; \
    /usr/local/bin/frpc --version || true; \
    /usr/local/bin/frps --version || true

# Stage 2.5: Install wg-obfuscator (cached layer)
FROM base AS obfuscator-bin
RUN set -eux; \
    ARCH_RAW="$(uname -m)"; \
    case "$ARCH_RAW" in \
        x86_64) ARCH="x64" ;; \
        aarch64) ARCH="arm64" ;; \
        armv7l) ARCH="armv7-hf" ;; \
        armv6l) ARCH="armv6-softfp" ;; \
        *) ARCH="x64" ;; \
    esac; \
    OBFUSCATOR_VERSION="1.5"; \
    OBFUSCATOR_TMP="$(mktemp -d)"; \
    OBFUSCATOR_URL="https://github.com/ClusterM/wg-obfuscator/releases/download/v${OBFUSCATOR_VERSION}/wg-obfuscator-v${OBFUSCATOR_VERSION}-linux-${ARCH}.tar.gz"; \
    curl -fsSL -L "$OBFUSCATOR_URL" -o "$OBFUSCATOR_TMP/wg-obfuscator.tar.gz"; \
    cd "$OBFUSCATOR_TMP"; \
    tar -xzf wg-obfuscator.tar.gz; \
    OBFUSCATOR_BIN="$(find . -type f -name 'wg-obfuscator' | head -n1)"; \
    if [ -n "$OBFUSCATOR_BIN" ]; then \
        install -Dm755 "$OBFUSCATOR_BIN" /usr/local/bin/wg-obfuscator; \
    fi; \
    rm -rf "$OBFUSCATOR_TMP"; \
    /usr/local/bin/wg-obfuscator --version || true

# Stage 3: Python dependencies (cached layer)
FROM base AS deps
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir -r requirements.txt

# Stage 4: Final image
FROM base
WORKDIR /app

ARG SMITE_VERSION=latest
LABEL org.opencontainers.image.version="${SMITE_VERSION}"
LABEL smite.version="${SMITE_VERSION}"
ENV SMITE_VERSION="${SMITE_VERSION}"

# Copy binaries from binary stages
COPY --from=frp-bin /usr/local/bin/frpc /usr/local/bin/frpc
COPY --from=frp-bin /usr/local/bin/frps /usr/local/bin/frps
COPY --from=obfuscator-bin /usr/local/bin/wg-obfuscator /usr/local/bin/wg-obfuscator

# Copy Python dependencies from deps stage
COPY --from=deps /usr/local /usr/local

# Copy application code (changes most frequently)
COPY . .

EXPOSE 8888

RUN echo '#!/bin/sh' > /app/start.sh && \
    echo 'exec uvicorn main:app --host 0.0.0.0 --port "${NODE_API_PORT:-8888}" --log-level info --access-log' >> /app/start.sh && \
    chmod +x /app/start.sh

CMD ["/bin/sh", "/app/start.sh"]
