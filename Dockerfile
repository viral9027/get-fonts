# Use a slim Python base image to reduce size
FROM python:3.9-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Install system dependencies required for Playwright and other libraries
RUN apt-get update && apt-get install -y \
    libx11-xcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxi6 \
    libxtst6 \
    libnss3 \
    libxrandr2 \
    libasound2 \
    libpangocairo-1.0-0 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libgbm1 \
    fonts-liberation \
    libu2f-udev \
    libvulkan1 \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Create the static directory to ensure it exists
RUN mkdir -p static
COPY . .
# Copy requirements file and install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN playwright install chromium

# Expose the port the app runs on
EXPOSE 8000
# Set environment variables for Flask
ENV FLASK_APP=app.py
ENV FLASK_ENV=production
# Simplified CMD: Run uvicorn with minimal options (single worker)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]