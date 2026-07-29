FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY bridge/ /app/bridge/
COPY test_fixtures/ /app/test_fixtures/

ENV BLACKBOARD=/blackboard FIXTURES=/app/test_fixtures PORT=9000

CMD ["python", "/app/bridge/mock.py"]
