# Turtle’s Chat

Turtle’s Chat is an OpenAI-compatible chat gateway and a focused Open WebUI
customization. It combines a Python gateway, a branded web client, provider
adapters, PostgreSQL-backed policy and history, and private object-storage
integration.

This public repository contains application source, tests, Dockerfiles and the
reproducible public build workflow. It intentionally excludes production
credentials, authentication captures, account data, internal operations
documents, deployment addresses, backups and runtime configuration.

## Security boundary

Never commit any of the following:

- `.env` files or production configuration
- cookies, access tokens, authentication JSON or browser profiles
- SSH keys, webhook secrets or cloud-storage credentials
- databases, logs, packet captures, release bundles or backups

Production configuration belongs on the deployment server. The GitHub workflow
uses an ephemeral repository token only to publish commit-addressed container
images to GitHub Container Registry. Production hosts pull those finished
images and perform their own validation, backup and blue-green switch.

## Development

Python 3.12+, `uv`, Node.js 22+ and Docker are recommended.

```bash
cp .env.example .env
uv sync --extra test --extra claude
uv run --extra test --extra claude pytest -q
docker compose up --build
```

Do not place real provider or storage credentials into `.env.example`.

## Images

Every successful push to `main` publishes immutable, `linux/amd64` images
tagged with the exact Git commit:

- `ghcr.io/turtle-li/turtle-chat-gateway:git-<commit>`
- `ghcr.io/turtle-li/turtle-chat-open-webui:git-<commit>`

The moving `main` tags are conveniences only. Production deployment must use
the commit-addressed tags and verify the OCI revision label.

## Upstream and terms

The project integrates and patches upstream open-source projects, including
Open WebUI and gpt4free. Their own licenses and notices continue to apply.
Operators are responsible for reviewing the terms of every upstream provider;
publishing this source does not grant permission to share, resell or automate
third-party accounts.

This repository does not currently declare a separate license for Turtle-owned
code.
