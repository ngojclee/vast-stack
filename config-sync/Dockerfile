FROM python:3.11-alpine

RUN apk add --no-cache openssh-client && \
    pip install --no-cache-dir pyyaml==6.0.2

COPY sync.py /sync.py

ENTRYPOINT ["python", "-u", "/sync.py"]
