# Use an official Python runtime as the base image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies for Playwright and other libraries
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
    && apt-get clean

# Set environment variables for Playwright to control binary location
ENV PLAYWRIGHT_BROWSERS_PATH=/app/playwright-browsers

# Install Playwright and its dependencies
RUN pip install playwright==1.44.0 && \
    playwright install --with-deps chromium && \
    mkdir -p /app/playwright-browsers

# Copy requirements file
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the static directory explicitly
COPY static/ ./static/

# Copy the rest of the application code
COPY . .

# Re-run playwright install to ensure binaries are available after copying code
RUN playwright install

# Expose the port Railway will use
EXPOSE 8000

# Command to run the FastAPI app
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]