# onec-vecgraph MCP server image. Build variants (see docs/DEPLOYMENT.md):
#
#   CPU local (default):  docker build -t onec-vecgraph .
#   GPU local (cu128):    docker build --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128 \
#                                       -t onec-vecgraph:gpu .
#   Cloud (no torch):     docker build --build-arg EXTRAS=cloud-embeddings -t onec-vecgraph:cloud .
#
# Heavy indexing/vectorization (`index`, `vectorize`, `callgraph`) is normally run OFFLINE; the
# served MCP only embeds the query string per search call — fine on CPU or via a cloud API. GPU
# images additionally need `--gpus all` + the NVIDIA Container Toolkit and a matching host driver.

FROM python:3.12-slim AS base

# EXTRAS: local-embeddings (torch+sentence-transformers) | cloud-embeddings (openai+voyageai)
ARG EXTRAS=local-embeddings
# Only used when EXTRAS includes local-embeddings: .../whl/cpu (default) or .../whl/cu128 (GPU).
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Container-appropriate defaults (override per deployment / compose).
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    NEO4J_URI=bolt://neo4j:7687 \
    EMBEDDING_PROVIDER=local \
    EMBEDDING_DEVICE=auto \
    HF_HOME=/models

WORKDIR /app

# `git` is needed to clone artifact/ITS source repos during ingest.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --upgrade pip

# Install dependencies against project metadata first (better layer caching), then the source.
COPY pyproject.toml README.md ./
COPY src ./src
# For local embeddings, install torch from the selected index FIRST so the package's torch>=2.7
# dep is already satisfied (pip won't pull the default PyPI build over it). Cloud builds skip torch.
# `ingest` extra (pyyaml) is always included so manifests work in any image variant.
RUN if echo "${EXTRAS}" | grep -q local-embeddings; then \
        pip install "torch>=2.7" --index-url "${TORCH_INDEX_URL}"; \
    fi && \
    pip install ".[${EXTRAS},ingest]"

# HF model cache lives on a mounted volume (first query downloads ~1.2 GB for Qwen3-Embedding-0.6B).
RUN mkdir -p /models && useradd -m -u 10001 app && chown -R app:app /app /models
USER app

# 8000 = read query server (default CMD); 8001 = opt-in overlay-write server (`serve-write`,
# launched via compose `command:` override — see docker-compose.yml `app-write`).
EXPOSE 8000 8001

# Liveness: verifies Neo4j connectivity (the server's hard dependency).
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=5 \
    CMD ["onec-vecgraph", "health"]

CMD ["onec-vecgraph", "serve", "--transport", "http"]
