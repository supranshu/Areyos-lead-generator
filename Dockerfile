FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY areyos ./areyos
COPY alembic ./alembic
COPY alembic.ini ./
RUN pip install --no-cache-dir .
ENV PYTHONUNBUFFERED=1
CMD ["areyos", "serve", "--host", "0.0.0.0"]
