# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies required for Playwright
RUN apt-get update && apt-get install -y \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements file
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN playwright install --with-deps chromium
RUN ls -la /root/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell || echo "Playwright executable not found!"
# Copy the rest of the application code
COPY . .

EXPOSE 5000

# Set environment variables (optional, adjust as needed)
ENV PYTHONUNBUFFERED=1

# Command to run your application
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "--graceful-timeout", "30", "--worker-class", "gevent", "wsgi:app"]