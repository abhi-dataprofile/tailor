# Playwright's official Python image ships Chromium + all system deps preinstalled,
# so the browser auto-apply engine works out of the box in a container.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

# deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# hosts inject $PORT; bind to all interfaces so the platform can reach us
ENV HOST=0.0.0.0
EXPOSE 8765

# web + API. Run the crawler/applier as a SEPARATE service (see render.yaml) so a
# slow crawl never blocks the web server.
CMD ["python3", "serve.py"]
