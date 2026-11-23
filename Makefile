.DEFAULT_GOAL := help

DEV_PORT ?= 19000

.PHONY: help dev
help:
	@printf '%s\n' 'make dev  启动 Python AI 服务'

dev:
	uv run --locked --extra characters --extra dialogue uvicorn app.main:app --host 127.0.0.1 --port "$(DEV_PORT)" --workers 1
