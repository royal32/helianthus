FROM python:3.13-alpine

RUN apk add --no-cache bash curl docker-cli docker-cli-compose iproute2

WORKDIR /stack

CMD ["python3", "/stack/scripts/configure-app-connections.py"]
