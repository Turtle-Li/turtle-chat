ARG OPEN_WEBUI_BASE_IMAGE=ghcr.io/open-webui/open-webui@sha256:72c0ba641ba75e7aa52655cb242570906ececd09b1140fb736483038a22b3228
FROM ${OPEN_WEBUI_BASE_IMAGE}

ENV ENABLE_VERSION_UPDATE_CHECK=false

USER root

COPY custom.css /app/build/static/custom.css
COPY model-controls.js /app/build/static/turtle-model-controls.js
COPY storage-controls.js /app/build/static/turtle-storage-controls.js
COPY client-read-cache.js /tmp/turtle-client-read-cache.js
COPY turtle_static.py /app/backend/open_webui/turtle_static.py
COPY admin-console.html /app/build/static/turtle-admin.html
COPY admin-console.css /app/build/static/turtle-admin.css
COPY admin-console.js /app/build/static/turtle-admin.js
COPY project-api.html /app/build/static/turtle-project-api.html
COPY project-api.css /app/build/static/turtle-project-api.css
COPY project-api.js /app/build/static/turtle-project-api.js
COPY turtle_database.py /app/backend/open_webui/turtle_database.py
COPY migrate_sqlite_to_postgres.py /app/backend/migrate_sqlite_to_postgres.py
COPY turtle_admin /app/backend/open_webui/turtle_admin
COPY turtle_auth /app/backend/open_webui/turtle_auth
COPY turtle_chat /app/backend/open_webui/turtle_chat
COPY turtle_project_api /app/backend/open_webui/turtle_project_api
COPY turtle_storage /app/backend/open_webui/turtle_storage
COPY patch_open_webui.py /tmp/patch_open_webui.py

COPY assets/favicon.png /app/build/static/favicon.png
COPY assets/favicon.png /app/build/static/favicon-dark.png
COPY assets/favicon-96x96.png /app/build/static/favicon-96x96.png
COPY assets/favicon.ico /app/build/static/favicon.ico
COPY assets/apple-touch-icon.png /app/build/static/apple-touch-icon.png
COPY assets/turtle-gpt-logo-192.png /app/build/static/web-app-manifest-192x192.png
COPY assets/turtle-gpt-logo-512.png /app/build/static/web-app-manifest-512x512.png
COPY assets/turtle-gpt-logo-512.png /app/build/static/logo.png
COPY assets/turtle-gpt-logo-512.png /app/build/static/splash.png
COPY assets/turtle-gpt-logo-512.png /app/build/static/splash-dark.png
COPY assets/turtle-gpt-logo.webp /app/build/static/turtle-gpt-logo.webp
COPY assets/provider-chatgpt.svg /app/build/static/turtle-provider-chatgpt.svg
COPY assets/provider-claude.svg /app/build/static/turtle-provider-claude.svg
COPY assets/favicon.png /app/build/favicon.png

RUN python /tmp/patch_open_webui.py \
    && rm /tmp/patch_open_webui.py /tmp/turtle-client-read-cache.js

LABEL org.opencontainers.image.title="Turtle's Chat Open WebUI"
LABEL org.opencontainers.image.description="Private Turtle's Chat brand layer for Open WebUI"
