FROM python:3.12.13-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv

RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /build
COPY backend/requirements.lock backend/pyproject.toml backend/README.md ./
RUN python -m pip install --requirement requirements.lock

COPY backend/src ./src
RUN python -m pip install --no-deps .

FROM python:3.12.13-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH \
    SKILLMIND_CONTRACTS_DIR=/app/contracts

# 資源快照の物化と repository.read/v1 は git/svn command を Provider 境界内で実行する
# (計画 §19 W4)。未導入だと repository 要求を持つ Run が実行時にしか失敗しないため、
# Worker/API が共有するこの runtime image へ同梱する。
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git subversion \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 skillmind \
    && useradd --uid 10001 --gid skillmind --create-home --home-dir /home/skillmind skillmind \
    && mkdir -p /app /var/lib/skillmind/runs \
    && chown -R skillmind:skillmind /app /var/lib/skillmind

COPY --from=builder /opt/venv /opt/venv
COPY contracts /app/contracts
COPY skills/skillmind-skill-interpreter /app/skills/skillmind-skill-interpreter
COPY backend/alembic.ini /app/backend/alembic.ini
COPY backend/migrations /app/backend/migrations
COPY scripts/compose.py scripts/deploy.py scripts/deploy_checks.py /opt/skillmind-deploy/

WORKDIR /app/backend
USER 10001:10001

CMD ["python", "-m", "uvicorn", "skillmind.api.main:app", "--host=0.0.0.0", "--port=8000"]
