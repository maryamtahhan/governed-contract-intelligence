FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src/hybrid_demo ./src/hybrid_demo
RUN pip install --no-cache-dir .

RUN mkdir /data && chown 1001:0 /data
ENV TRACE_DB_PATH=/data/traces.db
USER 1001

CMD ["hybrid-traceui"]
