FROM ngc-cuda:12.4.1-devel-ubuntu22.04

# ------------------------------------------------------------
# Base env
# ------------------------------------------------------------
ENV DEBIAN_FRONTEND=noninteractive \
    PYENV_ROOT="/root/.pyenv"

RUN apt-get update -yqq && apt-get install -yqq \
        python3 \
        python3-pip \
        curl \
        wget \
        tree \
        zip \
        unzip \
        git \
        jq \
        software-properties-common \
        build-essential \
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
    && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------------
# Pyenv + standalone Python builds (multi versions)
# ------------------------------------------------------------
ENV PATH="${PYENV_ROOT}/bin:${PYENV_ROOT}/shims:${PATH}"

COPY <<EOF /tmp/python-versions.csv
3.11.7,20240107
3.12.0,20231002
3.10.13,20240224
3.13.1,20241205
3.9.18,20240224
3.8.18,20240224
EOF

RUN set -eux; \
    git clone https://github.com/pyenv/pyenv.git "${PYENV_ROOT}"; \
    ARCH="$(uname -m)"; \
    DOWNLOADS="https://github.com/astral-sh/python-build-standalone/releases/download"; \
    while IFS="," read -r VERSION RELEASE; do \
        mkdir -p "${PYENV_ROOT}/versions/${VERSION}"; \
        wget --quiet "${DOWNLOADS}/${RELEASE}/cpython-${VERSION}+${RELEASE}-${ARCH}-unknown-linux-gnu-install_only.tar.gz" \
            -O "/tmp/${VERSION}.tar.gz"; \
        tar -xzf "/tmp/${VERSION}.tar.gz" \
            -C "${PYENV_ROOT}/versions/${VERSION}" \
            --strip-components=1; \
        ln -frs "${PYENV_ROOT}/versions/${VERSION}/bin/python${VERSION%.*}" \
                "${PYENV_ROOT}/versions/${VERSION}/bin/python"; \
        rm "/tmp/${VERSION}.tar.gz"; \
    done < /tmp/python-versions.csv; \
    pyenv global $(cut -d"," -f1 /tmp/python-versions.csv | tr "\n" " "); \
    pyenv rehash; \
    rm /tmp/python-versions.csv

# ------------------------------------------------------------
# Miniconda (base python = 3.11) + tools installed into conda base
# ------------------------------------------------------------
ENV CONDA_DIR=/opt/conda
RUN set -eux; \
    ARCH="$(uname -m)"; \
    wget --quiet "https://repo.anaconda.com/miniconda/Miniconda3-py311_25.3.1-1-Linux-${ARCH}.sh" -O /tmp/miniconda.sh; \
    /bin/bash /tmp/miniconda.sh -b -p "${CONDA_DIR}"; \
    rm /tmp/miniconda.sh; \
    "${CONDA_DIR}/bin/conda" config --system --set always_yes yes; \
    "${CONDA_DIR}/bin/conda" config --system --set changeps1 no; \
    "${CONDA_DIR}/bin/conda" update -n base -c defaults conda; \
    "${CONDA_DIR}/bin/conda" install -n base python=3.11 pip; \
    "${CONDA_DIR}/bin/conda" clean -afy

ENV PATH="${CONDA_DIR}/bin:${PATH}"

RUN set -eux; \
    python -V; \
    python -m pip install --no-cache-dir \
        uv \
        pyright \
        search-and-replace \
        pipenv

RUN set -eux; \
    curl -sSL https://install.python-poetry.org | python -; \
    ln -sf /root/.local/bin/poetry /usr/local/bin/poetry

# ------------------------------------------------------------
# Node.js (keep npm install for NexAU)
# ------------------------------------------------------------
RUN set -eux; \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -; \
    apt-get update -yqq; \
    apt-get install -yqq nodejs; \
    rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------------
# Workspace
# ------------------------------------------------------------
RUN mkdir -p /data/project
WORKDIR /data/project

# ------------------------------------------------------------
# NexAU: pull repo & build
# ------------------------------------------------------------
ARG NEXAU_GIT_URL="https://github.com/No-518/NexAU.git"

RUN git clone --depth 1 "${NEXAU_GIT_URL}" /opt/nexau
WORKDIR /opt/nexau

# Install NexAU + runtime deps into a venv using Python 3.12 from pyenv
RUN uv sync --frozen --no-dev -p /root/.pyenv/versions/3.12.0/bin/python

# Build the NexAU CLI
RUN cd cli && npm install && npm run build

# Use NexAU venv by default in interactive shells
ENV VIRTUAL_ENV="/opt/nexau/.venv" \
    PATH="/opt/nexau/.venv/bin:$PATH"

# Back to your working directory
WORKDIR /data/project

# IMPORTANT CHANGE: do NOT auto-start NexAU
# Default: open a shell (so you can manually start agent)
CMD ["/bin/bash"]
