FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/agent
WORKDIR /app

COPY agent/ /app/agent/
COPY bridge/ /app/bridge/
COPY souls/ /app/souls/
COPY test_fixtures/ /app/test_fixtures/

ENV BLACKBOARD=/blackboard FIXTURES=/app/test_fixtures MODEL_PROVIDER=mock

CMD ["python", "/app/agent/loop.py"]
