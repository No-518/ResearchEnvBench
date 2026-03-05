# syntax=docker/dockerfile:1


# ============================================================
# SciMLOpsBench base image
# - 统一 CUDA/OS/工具链基座
# - 不固化任何 repo 依赖/torch 版本/数据/权重
# - repo 级依赖由 agent 在容器内现配
# ============================================================

# Paper-aligned official CUDA base image.
FROM nvidia/cuda:12.4.1-devel-ubuntu22.04

SHELL ["/bin/bash", "-lc"]

# ------------------------------------------------------------
# Global env (align with M2/M4 conventions)
# ------------------------------------------------------------
ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Etc/UTC \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYENV_ROOT="/root/.pyenv" \
    CONDA_DIR="/opt/conda" \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    SCIMLOPSBENCH_REPORT="/opt/scimlopsbench/report.json"

# ------------------------------------------------------------
# OS deps (build tools + common runtime libs)
# ------------------------------------------------------------
RUN apt-get update -yqq && apt-get install -yqq --no-install-recommends \
      python3 \
      python3-pip \
      curl \
      wget \
      tree \
      zip \
      unzip \
      rsync \
      git \
      jq \
      time \
      procps \
      pciutils \
      ca-certificates \
      lsb-release \
      software-properties-common \
      build-essential \
      cmake \
      ninja-build \
      pkg-config \
      zlib1g-dev \
      libssl-dev \
      libffi-dev \
      libbz2-dev \
      libreadline-dev \
      libsqlite3-dev \
      liblzma-dev \
      libncurses5-dev \
      libncursesw5-dev \
      xz-utils \
      tk-dev \
      llvm \
      libxml2-dev \
      libxmlsec1-dev \
      ffmpeg \
      libgl1 \
      libglib2.0-0 \
      libsm6 \
      libxext6 \
      libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------------
# Pyenv + python-build-standalone (multi Python versions 3.8-3.13)
# ------------------------------------------------------------
ENV PATH="${PYENV_ROOT}/bin:${PYENV_ROOT}/shims:${PATH}"

RUN set -euxo pipefail; \
    # Force git to use HTTP/1.1 (avoid flaky HTTP/2 framing issues)
    git config --global http.version HTTP/1.1; \
    git config --global http.postBuffer 1048576000; \
    \
    # Robust clone with retries + shallow + blob filter
    for i in 1 2 3 4 5; do \
      rm -rf "${PYENV_ROOT}"; \
      if git clone --depth 1 --filter=blob:none https://github.com/pyenv/pyenv.git "${PYENV_ROOT}"; then \
        break; \
      fi; \
      echo "pyenv clone failed (attempt $i), retrying..." >&2; \
      sleep 5; \
    done; \
    test -d "${PYENV_ROOT}/.git"; \
    \
    ARCH="$(uname -m)"; \
    DOWNLOADS="https://github.com/astral-sh/python-build-standalone/releases/download"; \
    printf '%s\n' \
      '3.8.18,20240224' \
      '3.9.18,20240224' \
      '3.10.13,20240224' \
      '3.11.7,20240107' \
      '3.12.0,20231002' \
      '3.13.1,20241205' \
      > /tmp/python-versions.csv; \
    \
    # Download python-build-standalone tarballs with retries (wget)
    while IFS=',' read -r VERSION RELEASE; do \
        mkdir -p "${PYENV_ROOT}/versions/${VERSION}"; \
        URL="${DOWNLOADS}/${RELEASE}/cpython-${VERSION}+${RELEASE}-${ARCH}-unknown-linux-gnu-install_only.tar.gz"; \
        wget -t 5 --waitretry=2 --retry-connrefused --timeout=60 -q "${URL}" -O "/tmp/${VERSION}.tar.gz"; \
        tar -xzf "/tmp/${VERSION}.tar.gz" -C "${PYENV_ROOT}/versions/${VERSION}" --strip-components=1; \
        ln -frs "${PYENV_ROOT}/versions/${VERSION}/bin/python${VERSION%.*}" "${PYENV_ROOT}/versions/${VERSION}/bin/python"; \
        rm -f "/tmp/${VERSION}.tar.gz"; \
    done < /tmp/python-versions.csv; \
    \
    pyenv global $(cut -d',' -f1 /tmp/python-versions.csv | tr '\n' ' '); \
    pyenv rehash; \
    rm -f /tmp/python-versions.csv


# ------------------------------------------------------------
# Miniconda (base python=3.11) + Python tools (uv/poetry/pyright/pipenv)
# ------------------------------------------------------------
RUN set -euxo pipefail; \
    ARCH="$(uname -m)"; \
    wget --quiet "https://repo.anaconda.com/miniconda/Miniconda3-py311_25.3.1-1-Linux-${ARCH}.sh" -O /tmp/miniconda.sh; \
    /bin/bash /tmp/miniconda.sh -b -p "${CONDA_DIR}"; \
    rm -f /tmp/miniconda.sh; \
    "${CONDA_DIR}/bin/conda" config --system --set always_yes yes; \
    "${CONDA_DIR}/bin/conda" config --system --set changeps1 no; \
    "${CONDA_DIR}/bin/conda" update -n base -c defaults conda; \
    "${CONDA_DIR}/bin/conda" install -n base python=3.11 pip; \
    "${CONDA_DIR}/bin/conda" clean -afy

ENV PATH="${CONDA_DIR}/bin:${PATH}"

# Make python3 available (run_one_job.py uses python3 for report validation)
RUN ln -sf "${CONDA_DIR}/bin/python" /usr/local/bin/python3 && \
    ln -sf "${CONDA_DIR}/bin/pip" /usr/local/bin/pip3

RUN set -euxo pipefail; \
    python -V; \
    python -m pip install --no-cache-dir \
      uv \
      pyright \
      pipenv \
      search-and-replace

RUN set -euxo pipefail; \
    curl -sSL https://install.python-poetry.org | python -; \
    ln -sf /root/.local/bin/poetry /usr/local/bin/poetry

# ------------------------------------------------------------
# Node.js (often needed by agent tooling like NexAU CLI)
# ------------------------------------------------------------
RUN set -euxo pipefail; \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -; \
    apt-get update -yqq; \
    apt-get install -yqq --no-install-recommends nodejs; \
    rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------------
# Required work dirs (align with benchmark convention)
# ------------------------------------------------------------
RUN mkdir -p /data/project /data/results /opt/scimlopsbench && \
    chmod -R 777 /data/project /data/results /opt/scimlopsbench

WORKDIR /data/project

# ------------------------------------------------------------
# Non-root user (needed for --allow-dangerously-skip-permissions)
# ------------------------------------------------------------
ARG CLAUDE_USER=claude
ARG CLAUDE_UID=1000
ARG CLAUDE_GID=1000
RUN set -euxo pipefail; \
    if getent group "${CLAUDE_GID}" >/dev/null 2>&1; then \
        CLAUDE_GROUP="$(getent group "${CLAUDE_GID}" | cut -d: -f1)"; \
    else \
        groupadd -g "${CLAUDE_GID}" "${CLAUDE_USER}"; \
        CLAUDE_GROUP="${CLAUDE_USER}"; \
    fi; \
    if ! id -u "${CLAUDE_USER}" >/dev/null 2>&1; then \
        useradd -m -u "${CLAUDE_UID}" -g "${CLAUDE_GROUP}" -s /bin/bash "${CLAUDE_USER}"; \
    fi; \
    mkdir -p /opt/claude_config; \
    chown -R "${CLAUDE_USER}:${CLAUDE_GROUP}" /opt/claude_config


# ------------------------------------------------------------
# Codex CLI (needed for codex backend runner inside container)
# ------------------------------------------------------------
RUN set -euxo pipefail; \
    npm --version; \
    node --version; \
    npm i -g @openai/codex; \
    codex --version

# ------------------------------------------------------------
# Claude Code CLI (non-interactive download + verify)
# ------------------------------------------------------------
RUN set -euxo pipefail; \
    CLAUDE_BUCKET="https://storage.googleapis.com/claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/claude-code-releases"; \
    case "$(uname -m)" in \
        x86_64|amd64) arch="x64" ;; \
        arm64|aarch64) arch="arm64" ;; \
        *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    if [ -f /lib/libc.musl-x86_64.so.1 ] || [ -f /lib/libc.musl-aarch64.so.1 ] || ldd /bin/ls 2>&1 | grep -q musl; then \
        platform="linux-${arch}-musl"; \
    else \
        platform="linux-${arch}"; \
    fi; \
    version="$(curl -fsSL "${CLAUDE_BUCKET}/latest")"; \
    manifest_json="$(curl -fsSL "${CLAUDE_BUCKET}/${version}/manifest.json")"; \
    checksum="$(echo "${manifest_json}" | jq -r ".platforms[\"${platform}\"].checksum // empty")"; \
    if [ -z "${checksum}" ]; then echo "Platform ${platform} not found in manifest" >&2; exit 1; fi; \
    curl -fsSL "${CLAUDE_BUCKET}/${version}/${platform}/claude" -o /usr/local/bin/claude; \
    echo "${checksum}  /usr/local/bin/claude" | sha256sum -c -; \
    chmod +x /usr/local/bin/claude; \
    claude --version

# ------------------------------------------------------------
# (Optional) NexAU baseline baked into image
# If you don't need NexAU, you can delete this block.
# ------------------------------------------------------------
ARG NEXAU_GIT_URL="https://github.com/No-518/NexAU.git"
ARG NEXAU_REF="main"

RUN set -euxo pipefail; \
    git clone --depth 1 --branch "${NEXAU_REF}" "${NEXAU_GIT_URL}" /opt/nexau

WORKDIR /opt/nexau

# Install NexAU into its own venv (uses Python 3.12 from pyenv)
RUN set -euxo pipefail; \
    uv sync --frozen --no-dev -p /root/.pyenv/versions/3.12.0/bin/python; \
    cd cli && npm install && npm run build

WORKDIR /data/project

CMD ["/bin/bash"]
