FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the bot script
COPY lenskart_bot.py .

# The bot runs as a persistent background process (long polling).
# Auto-restart is handled inside the script.
CMD ["python3", "-u", "lenskart_bot.py"]
