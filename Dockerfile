FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY bot.py .
COPY razorpay_billing.py .

# Expose web server port
EXPOSE 8080

CMD ["python", "bot.py"]
