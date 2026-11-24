.DEFAULT_GOAL := help

DEV_PORT ?= 19000

.PHONY: help dev stop restart

help:
	@printf '%s\n' \
		'make dev      启动 Python AI 服务' \
		'make stop     停止占用开发端口的服务' \
		'make restart  先停止再启动'

dev:
	uv run --locked --extra characters --extra dialogue uvicorn app.main:app --host 127.0.0.1 --port "$(DEV_PORT)" --workers 1

stop:
	@pids="$$(lsof -nP -tiTCP:$(DEV_PORT) -sTCP:LISTEN 2>/dev/null || true)"; \
	if [ -z "$$pids" ]; then \
		printf '%s\n' "端口 $(DEV_PORT) 没有正在监听的服务"; \
		exit 0; \
	fi; \
	targets="$$pids"; \
	for pid in $$pids; do \
		parent="$$(ps -o ppid= -p "$$pid" 2>/dev/null | tr -d ' ')"; \
		while [ -n "$$parent" ] && [ "$$parent" != "1" ]; do \
			cmd="$$(ps -o command= -p "$$parent" 2>/dev/null || true)"; \
			case "$$cmd" in \
				*uvicorn*|*uv\ run*|*make*) targets="$$targets $$parent" ;; \
				*) break ;; \
			esac; \
			parent="$$(ps -o ppid= -p "$$parent" 2>/dev/null | tr -d ' ')"; \
		done; \
	done; \
	kill -CONT $$targets 2>/dev/null || true; \
	kill $$targets 2>/dev/null || true; \
	i=0; \
	while [ "$$i" -lt 25 ]; do \
		left="$$(lsof -nP -tiTCP:$(DEV_PORT) -sTCP:LISTEN 2>/dev/null || true)"; \
		if [ -z "$$left" ]; then \
			printf '%s\n' "已停止端口 $(DEV_PORT) 上的服务"; \
			exit 0; \
		fi; \
		i=$$((i + 1)); \
		sleep 0.1; \
	done; \
	kill -KILL $$targets $$left 2>/dev/null || true; \
	sleep 0.2; \
	left="$$(lsof -nP -tiTCP:$(DEV_PORT) -sTCP:LISTEN 2>/dev/null || true)"; \
	if [ -n "$$left" ]; then \
		printf '%s\n' "端口 $(DEV_PORT) 仍被占用"; \
		exit 1; \
	fi; \
	printf '%s\n' "已强制停止端口 $(DEV_PORT) 上的服务"

restart: stop
	@$(MAKE) dev
