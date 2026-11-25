FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY pii_rag ./pii_rag
RUN pip install --no-cache-dir .
ENV RAG_DATA_DIR=/app/.data
EXPOSE 8000
CMD ["uvicorn", "pii_rag.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
