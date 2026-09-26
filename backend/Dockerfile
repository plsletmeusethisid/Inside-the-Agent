FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Cache the tokenizer data during image build so runtime chunking does not fetch it.
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"
COPY app ./app
COPY sql ./sql
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
