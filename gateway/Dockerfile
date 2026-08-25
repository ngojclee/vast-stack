FROM python:3.11-alpine

RUN pip install --no-cache-dir \
    fastapi==0.116.1 \
    uvicorn==0.35.0 \
    httpx==0.28.1

COPY app.py /app.py

EXPOSE 18000

ENTRYPOINT ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "18000"]
