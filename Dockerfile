FROM python:3.12.15-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8
WORKDIR /app
COPY requirements-server.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt \
    && groupadd --gid 10001 sanwufan \
    && useradd --uid 10001 --gid 10001 --no-create-home sanwufan \
    && mkdir /data && chown 10001:10001 /data
COPY sanwufan ./sanwufan
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()"]
CMD ["python", "-m", "sanwufan.serve"]
