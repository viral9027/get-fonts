# Use a slim Python 3.12 base image for a smaller footprint
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies required for Playwright and other libraries
RUN apt-get update && apt-get install -y \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libxkbcommon0 \
    libgbm1 \
    libasound2 \
    fonts-noto \
    && rm -rf /var/lib/apt/lists/*

# Copy application files
COPY app.py extract_fonts.py req.txt ./
COPY templates/index.html templates/login.html ./templates/

# Install Python dependencies from req.txt
RUN pip install --no-cache-dir -r req.txt

# Install Playwright browsers
RUN playwright install --with-deps chromium

# Install gunicorn for production-grade WSGI server
RUN pip install gunicorn

# Expose port 5000 for Flask
EXPOSE 5000

# Set environment variables for Flask
ENV FLASK_APP=app.py
ENV FLASK_ENV=production

# Command to run the application with gunicorn
CMD ["gunicorn", "--timeout", "120", "--bind", "0.0.0.0:5000", "--workers", "4", "app:app"]