# Dockerfile
FROM python:3.11-slim

# Install necessary libraries
RUN pip install --no-cache-dir python-telegram-bot telethon

# Copy code
WORKDIR /app
COPY . /app

# (Optionally install any other OS dependencies if needed)
# Example: RUN apt-get update && apt-get install -y libssl-dev

# Expose port for Koyeb health checks
EXPOSE 8080

# Run the bot
CMD ["python", "bot.py"]
