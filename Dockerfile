# Production image: the built SPA and the API on one port, like run.py on the box.
# Deployed by github.com/nikhil1231/infra (stacks/holafresca), which mounts /data.

FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --silent
COPY frontend/ ./
RUN npm run build

# Playwright's image ships Chromium + its system deps (Ocado login and session).
# Keep PLAYWRIGHT_VERSION equal to the image tag.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
ARG PLAYWRIGHT_VERSION=1.63.0
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "playwright==${PLAYWRIGHT_VERSION}"
COPY . .
COPY --from=frontend /frontend/dist ./frontend/dist

ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8100
EXPOSE 8100
CMD ["python", "run.py"]
