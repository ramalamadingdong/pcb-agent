# Container-first: when the pcb-agent image exists, headless targets run
# inside it (see run.sh); otherwise they run against local tools, same as
# ever. `make setup` builds the image and fetches the Freerouting jar for
# native runs. Inside the container PCB_AGENT_CONTAINER is set, which
# disables the redirection — no docker-in-docker.

IMAGE ?= pcb-agent

# The pinned toolchain. These are the source of truth: setup passes them to
# docker build and the jar fetcher. The Dockerfile carries matching defaults
# only so a bare `docker build` works — bump both together. KICAD_BASE must
# match the KiCad major you review boards with (see the Dockerfile header).
KICAD_BASE := kicad/kicad:10.0@sha256:182c8005cb775a2c448a4c18681d489f1ff472a761885eba3e08b07e3c0564de
export FREEROUTING_VERSION := 2.3.0
export FREEROUTING_SHA256  := 3cf18d608437740bc497db6b8ef5888e2e60a08de0def20691d1bad0c0e0ee24

ifeq ($(PCB_AGENT_CONTAINER),)
HAVE_IMAGE := $(shell docker image inspect $(IMAGE) >/dev/null 2>&1 && echo yes)
endif
RUN := $(if $(HAVE_IMAGE),./run.sh )

.PHONY: doctor build route check clean setup

setup:
	docker build -t $(IMAGE) \
		--build-arg KICAD_BASE=$(KICAD_BASE) \
		--build-arg FREEROUTING_VERSION=$(FREEROUTING_VERSION) \
		--build-arg FREEROUTING_SHA256=$(FREEROUTING_SHA256) \
		--build-arg PUID=$(shell id -u) \
		--build-arg PGID=$(shell id -g) \
		.
	bash scripts/fetch-freerouting.sh tools

doctor:
	@$(RUN)python3 scripts/doctor.py

check:
	@$(RUN)python3 scripts/validate_gerbers.py ./fab/ -c board.toml

# --- these need the build passes dropped into scripts/build/ ---
# See scripts/build/CONTRACT.md for the interface each one must satisfy.
# When they land, wire them exactly like doctor/check above — prefix with
# $(RUN) and they run in the container when it exists.

build:
	@echo "Build passes not installed. See scripts/build/CONTRACT.md" && exit 1

route:
	@echo "Route script not installed. See scripts/build/CONTRACT.md" && exit 1

clean:
	rm -rf fab/ build/
