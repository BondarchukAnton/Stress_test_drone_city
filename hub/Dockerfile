FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/agent
WORKDIR /app

COPY hub/ /app/hub/
COPY agent/ /app/agent/
COPY test_fixtures/ /app/test_fixtures/

ENV BLACKBOARD=/blackboard FIXTURES=/app/test_fixtures PORT=8080 WEB_DIR=/app/hub/static

CMD ["python", "/app/hub/server.py"]
