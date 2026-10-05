# Використовуємо офіційний Python 3.12 на базі Debian 12 (bookworm)
FROM python:3.12-slim-bookworm

# --- КРИТИЧНО ДЛЯ MCP: Забезпечує вивід логів у stderr і взаємодію через stdin/stdout ---
ENV PYTHONUNBUFFERED=1

# Встановлюємо робочу директорію
WORKDIR /app

# Встановлюємо системні залежності для драйверів баз даних
# netcat-openbsd доданий для діагностики TCP-з'єднань всередині контейнера
RUN apt-get update && apt-get install -y --no-install-recommends \
    unixodbc-dev \
    gcc \
    libpq-dev \
    g++ \
    netcat-openbsd \
    # Очищуємо кеш apt для зменшення розміру образу
    && rm -rf /var/lib/apt/lists/*

# Копіюємо файл залежностей
COPY requirements.txt .

# Встановлюємо пакети Python
RUN pip install --no-cache-dir --trusted-host pypi.python.org -r requirements.txt

# Копіюємо решту коду застосунку
COPY . .

# Виводимо список файлів для діагностики
RUN ls -R /app

# Встановлюємо залежності для окремих модулів (якщо вони існують)
RUN for d in /app/modules/*/ ; do \
      if [ -f "${d}requirements.txt" ]; then \
        echo ">>> Installing dependencies for $(basename ${d})"; \
        pip install --no-cache-dir --trusted-host pypi.python.org -r "${d}requirements.txt"; \
      fi; \
    done

# Визначаємо точку входу (запуск головного скрипта)
ENTRYPOINT ["python", "main.py"]