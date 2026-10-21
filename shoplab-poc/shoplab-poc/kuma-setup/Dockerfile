FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uptime-kuma-api==1.2.1
COPY setup_kuma.py .
CMD ["python", "-u", "setup_kuma.py"]
