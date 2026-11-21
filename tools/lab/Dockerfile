FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
RUN apt-get update && apt-get install -y --no-install-recommends patch git && rm -rf /var/lib/apt/lists/*
COPY lab/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock && pip install --no-cache-dir coverage==7.10.7 pytest-cov==7.0.0
COPY sqlglot /src/sqlglot
COPY lab/brtrace.py /lab/brtrace.py
ENV PYTHONPATH=/src/sqlglot:/lab SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1
WORKDIR /src/sqlglot
