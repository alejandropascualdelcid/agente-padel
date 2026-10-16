FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY monitor_padel.py config.json ./
# config.json del repo es la configuración inicial; los cambios del bot se guardan en /data
ENV STATE_FILE=/data/state.json CONFIG_FILE=/data/config.json PYTHONUNBUFFERED=1
VOLUME ["/data"]
CMD ["python", "monitor_padel.py", "--loop"]
