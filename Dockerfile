FROM python:3.10-slim

WORKDIR /app

# Copy and install requirements first (this makes building faster)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your bot's code
COPY . .

# Start the bot
CMD ["python", "bot.py"]
