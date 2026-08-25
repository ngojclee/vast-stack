FROM python:3.11-alpine

RUN apk add --no-cache openssh-client

COPY tunnel_manager.py /tunnel_manager.py

ENTRYPOINT ["python", "-u", "/tunnel_manager.py"]
