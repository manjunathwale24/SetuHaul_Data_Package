FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY *.py *.sql ./
COPY dashboard.html deployment_dashboard.html ./
COPY tests ./tests

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data

USER 10001:10001

EXPOSE 8080

CMD ["python", "service.py", "--host", "0.0.0.0", "--port", "8080", "--db", "/data/setuhaul.sqlite"]
