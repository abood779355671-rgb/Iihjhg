FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot
COPY bot.py .

# Non-root user for security
RUN useradd -m botuser && chown -R botuser /app
USER botuser

CMD ["python", "-u", "bot.py"]
