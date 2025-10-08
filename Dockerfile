# Use lightweight Python image
FROM python:3.11-slim

# Set work directory
WORKDIR /app

# Copy files
COPY . /app

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose health-check port (for Koyeb)
EXPOSE 8080

# Run the bot
CMD ["python", "bot.py"]
