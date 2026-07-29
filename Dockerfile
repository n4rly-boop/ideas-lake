# Ideas Lake, блок A. Один образ: HTTP-слой, ингест и загрузчик в Neo4j — это
# один пакет `lake`, отдельные образы им не нужны.
FROM python:3.12-slim

# Торч ставится из CPU-индекса намеренно: дефолтное колесо для linux/amd64 тянет
# библиотеки CUDA (~2 ГБ), а эмбеддинги здесь считаются на CPU (`10` §3.2).
RUN pip install --no-cache-dir \
        torch==2.9.1 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir \
        fastapi==0.135.3 uvicorn==0.43.0 pydantic==2.12.5 numpy==2.4.3 \
        sentence-transformers==5.3.0 PyYAML==6.0.3 httpx==0.28.1 neo4j==6.2.0

# Модель кладётся в образ, а не качается на старте. Иначе первый запрос ждёт
# скачивание, а контейнер без сети не поднимается вообще — при том что
# `create_app` греет энкодер ДО того, как порт начнёт принимать (`api/app.py:76-80`).
ENV HF_HOME=/opt/hf
RUN python -c "from sentence_transformers import SentenceTransformer; \
               SentenceTransformer('Snowflake/snowflake-arctic-embed-s')"

WORKDIR /app
COPY lake/ /app/lake/

# `data/` — том, а не слой образа: там результаты живых прогонов (`10` §2).
VOLUME ["/app/lake/data"]
EXPOSE 8077

# Ключи и адрес графа приходят из окружения, в образ не попадают (`10` §2).
CMD ["python", "-m", "lake.api.app", "--host", "0.0.0.0", "--port", "8077"]
