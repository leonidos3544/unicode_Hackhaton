FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv==0.10.3
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY app ./app
COPY migrations ./migrations
COPY config ./config
COPY data/open ./data/open
COPY alembic.ini ./
RUN useradd --create-home appuser
USER appuser
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "python", "-m", "app"]
