# Use an official Python runtime as the base image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies for Playwright and woff2
RUN apt-get update && apt-get install -y \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libwoff1 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy requirements file
COPY requirements.txt .

# Upgrade pip and install Python dependencies
RUN pip install --no-cache-dir --upgrade pip==25.1.1 && \
    pip install --no-cache-dir -r requirements.txt

# Install Playwright and its dependencies
RUN pip install --no-cache-dir playwright==1.48.0 && \
    playwright install --with-deps chromium

# Install gunicorn for production-grade WSGI server
RUN pip install --no-cache-dir gunicorn==22.0.0

# Copy the rest of the application code
COPY . .
# Expose the port Railway will use
EXPOSE 5000

# Command to run the FastAPI app
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5000", "--workers", "5"]
