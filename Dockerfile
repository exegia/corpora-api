FROM rust:1.91-bookworm AS core-builder

ARG CORPORA_CORE_REF=2291a4a6706bc488ee3acd709a128759c363dd2b
RUN git clone https://github.com/exegia/corpora-core.git /core
WORKDIR /core
RUN git checkout "$CORPORA_CORE_REF" && \
    cargo build --locked --release -p corpora-core --features spellcheck --bin corpora-text-check

FROM python:3.12-slim-bookworm
WORKDIR /app
COPY --from=core-builder /core/target/release/corpora-text-check /usr/local/bin/corpora-text-check
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN pip install --no-cache-dir uv && uv sync --locked --no-dev --no-editable
ENV CORPORA_TEXT_CHECK_BIN=/usr/local/bin/corpora-text-check
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "corpora_api.app:app", "--host", "0.0.0.0", "--port", "8000"]
