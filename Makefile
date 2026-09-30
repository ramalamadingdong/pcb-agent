# Container-first: when the pcb-agent image exists, headless targets run
# inside it (see run.sh); otherwise they run against local tools, same as
# ever. `make setup` builds the image and fetches the Freerouting jar for
# native runs. Inside the container PCB_AGENT_CONTAINER is set, which
# disables the redirection — no docker-in-docker.
#
# Point BOARD_DIR at the directory holding board.toml + netlist.csv.
# The example board is the default — it is what a first-time user builds.

IMAGE ?= pcb-agent
BOARD_DIR ?= examples/unoq-power-shield
# `=`, not `?=`: WSL exports NAME (the machine's name) into every shell, and
# `?=` took it, so a build named its files after the computer. A NAME=... on
# the make command line still overrides this.
NAME = $(notdir $(abspath $(BOARD_DIR)))
CONFIG ?= $(BOARD_DIR)/board.toml
NETLIST ?= $(BOARD_DIR)/netlist.csv
SCH ?= $(BOARD_DIR)/$(NAME).kicad_sch
PCB ?= $(BOARD_DIR)/$(NAME).kicad_pcb
SNAPSHOT ?= $(BOARD_DIR)/pre_route.kicad_pcb
ROUNDS ?= 4
PASSES ?= 100
REPORT ?= $(BOARD_DIR)/build/route.json

# The pinned toolchain. These are the source of truth: setup passes them to
# docker build and the jar fetcher. The Dockerfile carries matching defaults
# only so a bare `docker build` works — bump both together. KICAD_BASE must
# match the KiCad major you review boards with (see the Dockerfile header).
KICAD_BASE := kicad/kicad:10.0@sha256:182c8005cb775a2c448a4c18681d489f1ff472a761885eba3e08b07e3c0564de
export FREEROUTING_VERSION := 1.9.0
export FREEROUTING_SHA256  := 9084a4888937a7f31f857ecc12aa7a37407f51160e4d2892dff9c9bb47ae3102

ifeq ($(PCB_AGENT_CONTAINER),)
HAVE_IMAGE := $(shell docker image inspect $(IMAGE) >/dev/null 2>&1 && echo yes)
endif
RUN := $(if $(HAVE_IMAGE),./run.sh )
P := python3 scripts/build

.PHONY: doctor build route export check check-placement drc erc render score clean setup

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

# netlist.csv -> schematic -> board -> placement -> zones -> marks -> silk,
# ending in the pre-route snapshot. Every step, the snapshot copy included,
# goes through $(RUN): with BOARD_DIR=/board the paths exist only inside the
# container. Order is load-bearing: mounting holes
# before the placement loop (their courtyards are obstacles), fiducials
# before the pour refill (the pour must honour their clearance ring),
# keepouts before silk (silk avoids the declared rectangles). direct_connect
# runs twice: `pre` snaps Direct-tagged parts onto anchored targets so
# place.py can hold them, `post` snaps the rest once the optimiser and
# flip_sides have put their targets where they stay. fix_pad_angles
# runs a second time before silk as insurance: the committed example has 16
# stripped pad angles from an earlier build (see that pass).
build:
	$(RUN)$(P)/make_libs.py --netlist $(NETLIST) --config $(CONFIG)
	$(RUN)$(P)/generate_schematic.py --schematic $(SCH) --netlist $(NETLIST) --config $(CONFIG)
	$(RUN)$(P)/create_pcb.py --schematic $(SCH) --board $(PCB) --netlist $(NETLIST) --config $(CONFIG)
	$(RUN)$(P)/apply_netclasses.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG)
	$(RUN)$(P)/floorplan.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG)
	$(RUN)$(P)/add_mounting_holes.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG)
	$(RUN)$(P)/direct_connect.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG) --stage pre
	$(RUN)$(P)/place.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG) --rounds $(ROUNDS)
	$(RUN)$(P)/flip_sides.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG)
	$(RUN)$(P)/direct_connect.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG) --stage post
	$(RUN)$(P)/check_placement.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG) --warn-only
	$(RUN)$(P)/zones.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG)
	$(RUN)$(P)/fanout.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG)
	$(RUN)$(P)/add_fiducials.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG)
	$(RUN)$(P)/add_keepouts.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG)
	$(RUN)$(P)/fix_pad_angles.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG)
	$(RUN)$(P)/silk_finish.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG)
	$(RUN)$(P)/zones.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG) --fill-only
	$(RUN)cp $(PCB) $(SNAPSHOT)
	@echo "snapshot: $(SNAPSHOT)"

# Snapshot -> Freerouting (xvfb, -mt 1, foreground) -> completion stack ->
# DRC x5, GATED against the [[drc.accept]] ledger. Re-runnable without paying
# for a rebuild: it always starts from the snapshot. Its JSON also lands in
# $(REPORT) for `make render` and `make score`.
route:
	$(RUN)$(P)/route.py --board $(PCB) --snapshot $(SNAPSHOT) --netlist $(NETLIST) --config $(CONFIG) --passes $(PASSES) --report $(REPORT)

export:
	$(RUN)$(P)/export_fab.py --board $(PCB) --config $(CONFIG)

# Levels 1, 2 and 4, each gated. ERC on the schematic; pad angles, DRC x5
# (on a filled COPY -- check writes nothing) and placement on the board;
# then the fab package, including its IPC-D-356 netlist against netlist.csv.
# The board-side checks can name what they found (`R12 pad 2`); the gerber
# checker reads the bytes the fab will plot and cannot. A pass on the board
# and a fail on the package means the export moved something -- which is the
# whole point of having both. `-k` semantics: run every gate, fail at the end.
check:
	@fail=0; \
	$(RUN)$(P)/erc_check.py --schematic $(SCH) --netlist $(NETLIST) --config $(CONFIG) >/dev/null || fail=1; \
	$(RUN)$(P)/fix_pad_angles.py --check --board $(PCB) --netlist $(NETLIST) --config $(CONFIG) >/dev/null || fail=1; \
	$(RUN)$(P)/drc_sample.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG) >/dev/null || fail=1; \
	$(RUN)$(P)/check_placement.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG) >/dev/null || fail=1; \
	$(RUN)python3 scripts/validate_gerbers.py $(BOARD_DIR)/fab -c $(CONFIG) --netlist $(NETLIST) || fail=1; \
	exit $$fail

# Connector accessibility + copper in keepouts, straight off the board file.
# Runs warn-only inside `build` (where the board is not final) and hard here.
check-placement:
	@$(RUN)$(P)/check_placement.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG)

# The individual gates, for iterating on one of them.
drc:
	@$(RUN)$(P)/drc_sample.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG)

erc:
	@$(RUN)$(P)/erc_check.py --schematic $(SCH) --netlist $(NETLIST) --config $(CONFIG)

# Pictures for review (level 3, /improve-placement): per copper layer, with
# the ratsnest in red and what the completion passes finished in orange, plus
# KiCad's own plots in review/kicad/. Writes nothing to the board.
render:
	@$(RUN)$(P)/render_review.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG) --out $(BOARD_DIR)/review --route-report $(REPORT)

# One comparable number for a routed board (see score_route.py). Pass
# BASELINE=path/to/score.json to compare.
score:
	@$(RUN)$(P)/score_route.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG) --out $(BOARD_DIR)/build/score.json --route-report $(REPORT) $(if $(BASELINE),--baseline $(BASELINE))

clean:
	$(RUN)rm -rf $(BOARD_DIR)/fab $(BOARD_DIR)/build $(BOARD_DIR)/review
