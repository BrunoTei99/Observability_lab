FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir httpx==0.27.2
COPY simulator.py .
CMD ["python", "-u", "simulator.py"]
