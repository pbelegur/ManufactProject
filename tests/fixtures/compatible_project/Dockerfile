FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8000
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py ./
RUN useradd --uid 10001 --create-home mcpuser
USER mcpuser
EXPOSE 8000
STOPSIGNAL SIGTERM
CMD ["python", "main.py"]
