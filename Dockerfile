FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY monitor_padel.py .
ENV STATE_FILE=/data/state.json PYTHONUNBUFFERED=1
VOLUME ["/data"]
CMD ["python", "monitor_padel.py", "--loop"]
