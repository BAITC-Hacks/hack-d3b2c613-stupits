# «Аким на 5 часов» — альтернативный запуск в Docker:
#   docker build -t akim5h .
#   docker run --rm -p 8501:8501 --env-file .env akim5h      # затем откройте http://localhost:8501
# Ключ API в образ не попадает (.env исключён в .dockerignore) — он передаётся при запуске.
# Без --env-file приложение работает целиком, советник — в офлайн-режиме.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Сначала только зависимости — этот слой кэшируется и не пересобирается при правках кода
COPY requirements.txt requirements-ui.txt ./
RUN pip install -r requirements.txt -r requirements-ui.txt

COPY . .

# Приложение не требует прав root
RUN useradd --create-home akim && chown -R akim /app
USER akim

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=2)"

CMD ["python", "-m", "streamlit", "run", "app.py", \
     "--server.address=0.0.0.0", "--server.port=8501", \
     "--server.headless=true", "--browser.gatherUsageStats=false"]
