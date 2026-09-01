FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# зависимости кешируются отдельным слоем
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# код проекта
COPY . .

# директория для SQLite-базы (маунтится через -v /opt/liza_data:/data)
RUN mkdir -p /data

CMD ["python", "main.py"]
