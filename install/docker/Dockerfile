# Build environment for install/install.sh on hosts that are not Linux x86-64 (macOS, Windows with Docker Desktop) or
# that lack the packages. The otatools are x86-64 Linux programs, so the image is always linux/amd64 (emulated on
# Apple Silicon). Used by install/docker-install.sh; the repository itself is mounted read-only at /repo.
FROM --platform=linux/amd64 debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl python3 lz4 dosfstools mtools openssl tar zstd diffutils git file \
      make perl flex bison libelf-dev libdw-dev libssl-dev \
      qemu-system-x86 ovmf \
 && rm -rf /var/lib/apt/lists/*
ENV GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=/repo
WORKDIR /work
