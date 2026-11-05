FROM python:3.13-slim
LABEL org.opencontainers.image.source="https://github.com/timzilla/char" \
      org.opencontainers.image.description="Char: a home print service for an 80 mm receipt printer" \
      org.opencontainers.image.licenses="MIT"
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ /app/
RUN mkdir -p /data /config/plugins
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
