FROM python:3.12-alpine
RUN pip install --no-cache-dir anthropic==2.3.0
WORKDIR /app
COPY server.py index.html ./
ENV PV_DB=/data/palabra.db
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"
CMD ["python3", "-u", "server.py"]
