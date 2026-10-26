FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOST=0.0.0.0 PORT=8000
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py ./
RUN useradd --uid 10001 --create-home mcpuser
USER mcpuser
EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=3s --timeout=2s --start-period=10s --retries=20 CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/health', timeout=1).close()"
CMD ["python", "server.py"]
