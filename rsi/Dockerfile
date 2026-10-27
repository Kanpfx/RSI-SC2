FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends bash tmux git \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml /tmp/pyproject.toml
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('/tmp/pyproject.toml', 'rb'))['project']['dependencies']))" > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt

ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
