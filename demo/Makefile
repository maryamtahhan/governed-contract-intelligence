# Stage dashboard (hybrid-ui) lifecycle.
#
#   make dashboard-up          replay only, bound to 127.0.0.1 (nothing live)
#   make dashboard-live        + one-document live runs on a localhost endpoint
#   make dashboard-down
#
# Live mode bound beyond localhost needs a token, because the live routes
# proxy to the endpoint (the page asks each visitor for it once):
#   make dashboard-live HOST=0.0.0.0 LIVE_TOKEN=sesame
#
# Inputs can be overridden, e.g. show another measured run:
#   make dashboard-up RUN=../runs/rosa-200-2026-11-10-praxis-routes/all-gpu COMPARE=   # one-pool control
#   make dashboard-live ENDPOINT=http://127.0.0.1:9000/v1
#   make dashboard-up PORT=8100

PORT     ?= 8099
HOST     ?= 127.0.0.1
RUN      ?= ../runs/rosa-200-2026-11-10-praxis-routes/hybrid
DATA     ?= data/synthetic
COMPARE  ?= ../runs/rosa-200-2026-11-10-praxis-routes/all-gpu
RUNS_DIR ?= ../runs
# Live mode: the OpenAI-compatible base URL the live routes proxy to, and the
# token that gates them (X-Live-Token / Authorization: Bearer; also read from
# HYBRID_UI_LIVE_TOKEN). Live mode on a non-loopback HOST refuses to start
# without a token.
ENDPOINT   ?=
LIVE_TOKEN ?=

PIDFILE  ?= .dashboard.pid
LOGFILE  ?= .dashboard.log

COMPARE_ARG  = $(if $(strip $(COMPARE)),--compare $(COMPARE),)
RUNS_ARG     = $(if $(strip $(RUNS_DIR)),--runs-dir $(RUNS_DIR),)
ENDPOINT_ARG = $(if $(strip $(ENDPOINT)),--endpoint $(ENDPOINT),)
TOKEN_ARG    = $(if $(strip $(LIVE_TOKEN)),--live-token $(LIVE_TOKEN),)

.PHONY: dashboard-up dashboard-live dashboard-down

dashboard-up:
	@if [ -f $(PIDFILE) ] && kill -0 $$(cat $(PIDFILE)) 2>/dev/null; then \
		echo "dashboard already running on http://$(HOST):$(PORT)/ (pid $$(cat $(PIDFILE)))"; \
		exit 0; \
	fi; \
	if lsof -ti tcp:$(PORT) >/dev/null 2>&1; then \
		echo "port $(PORT) is already in use; stop it or set PORT=<other>"; \
		exit 1; \
	fi; \
	nohup uv run hybrid-ui --run $(RUN) --data $(DATA) $(COMPARE_ARG) $(RUNS_ARG) \
		$(ENDPOINT_ARG) $(TOKEN_ARG) \
		--host $(HOST) --port $(PORT) > $(LOGFILE) 2>&1 & echo $$! > $(PIDFILE); \
	for i in $$(seq 1 40); do \
		if ! kill -0 $$(cat $(PIDFILE)) 2>/dev/null; then \
			echo "dashboard exited during startup; last log lines:"; \
			tail -20 $(LOGFILE); \
			rm -f $(PIDFILE); \
			exit 1; \
		fi; \
		if curl -sf http://$(HOST):$(PORT)/api/health >/dev/null 2>&1; then \
			echo "dashboard up: http://$(HOST):$(PORT)/ (pid $$(cat $(PIDFILE)))"; \
			echo "  run:     $(RUN)"; \
			echo "  compare: $(if $(strip $(COMPARE)),$(COMPARE),off)"; \
			echo "  live:    $(if $(strip $(ENDPOINT)),$(ENDPOINT),off (replay only))"; \
			echo "  token:   $(if $(strip $(LIVE_TOKEN)$(HYBRID_UI_LIVE_TOKEN)),required,off)"; \
			echo "  monitor: http://$(HOST):$(PORT)/monitor"; \
			exit 0; \
		fi; \
		sleep 0.5; \
	done; \
	echo "dashboard failed to start; last log lines:"; \
	tail -20 $(LOGFILE); \
	kill $$(cat $(PIDFILE)) 2>/dev/null || true; \
	rm -f $(PIDFILE); \
	exit 1

# The same dashboard with live runs enabled, pointed at a localhost endpoint
# by default. A non-loopback HOST additionally needs LIVE_TOKEN (see header);
# without a token anywhere the server still starts, localhost-only, and logs
# an unauthenticated-live warning.
dashboard-live: ENDPOINT = http://127.0.0.1:8000/v1
dashboard-live: dashboard-up

dashboard-down:
	@stopped=0; \
	if [ -f $(PIDFILE) ]; then \
		pid=$$(cat $(PIDFILE)); \
		if kill -0 $$pid 2>/dev/null; then kill $$pid 2>/dev/null && stopped=1; fi; \
		rm -f $(PIDFILE); \
	fi; \
	for pid in $$(lsof -ti tcp:$(PORT) 2>/dev/null); do \
		kill $$pid 2>/dev/null && stopped=1; \
	done; \
	for i in $$(seq 1 20); do \
		lsof -ti tcp:$(PORT) >/dev/null 2>&1 || break; \
		sleep 0.25; \
	done; \
	for pid in $$(lsof -ti tcp:$(PORT) 2>/dev/null); do kill -9 $$pid 2>/dev/null || true; done; \
	if [ "$$stopped" = "1" ]; then \
		echo "dashboard stopped (port $(PORT))"; \
	else \
		echo "dashboard not running (port $(PORT))"; \
	fi
