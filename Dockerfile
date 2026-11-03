FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY bonoxauto ./bonoxauto
RUN pip install --no-cache-dir .

VOLUME ["/app/data"]
CMD ["python", "-m", "bonoxauto"]
