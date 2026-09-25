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
NAME ?= $(notdir $(abspath $(BOARD_DIR)))
CONFIG ?= $(BOARD_DIR)/board.toml
NETLIST ?= $(BOARD_DIR)/netlist.csv
SCH ?= $(BOARD_DIR)/$(NAME).kicad_sch
PCB ?= $(BOARD_DIR)/$(NAME).kicad_pcb
SNAPSHOT ?= $(BOARD_DIR)/pre_route.kicad_pcb
KICAD_NET ?= $(BOARD_DIR)/$(NAME)-netlist.kicad_net
RELEASE ?= $(lastword $(sort $(wildcard $(BOARD_DIR)/release/rev-*)))
ROUNDS ?= 4
PASSES ?= 100

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

.PHONY: doctor build route export check check-placement parity release verify-release test-parity clean setup

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
# ending in the pre-route snapshot. Order is load-bearing: mounting holes
# before the placement loop (their courtyards are obstacles), fiducials
# before the pour refill (the pour must honour their clearance ring),
# keepouts before silk (silk avoids the declared rectangles). direct_connect
# runs twice: `pre` snaps Direct-tagged parts onto anchored targets so
# place.py can hold them, `post` snaps the rest once the optimiser and
# flip_sides have put their targets where they stay. link_schematic runs
# last, after every pass that adds a footprint and after the kct fill: it
# ties each footprint to its schematic symbol so the project a human opens
# is one design to KiCad, not two files that happen to agree.
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
	$(RUN)$(P)/silk_finish.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG)
	$(RUN)$(P)/zones.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG) --fill-only
	$(RUN)$(P)/link_schematic.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG)
	cp $(PCB) $(SNAPSHOT)
	@echo "snapshot: $(SNAPSHOT)"

# Snapshot -> Freerouting (xvfb, -mt 1, foreground) -> completion stack ->
# DRC x5 (compare violation kinds, not counts). Re-runnable without paying
# for a rebuild: it always starts from the snapshot.
route:
	$(RUN)$(P)/route.py --board $(PCB) --snapshot $(SNAPSHOT) --netlist $(NETLIST) --config $(CONFIG) --passes $(PASSES)

# Refuses to plot unless check_parity passes on the board being plotted.
export:
	$(RUN)$(P)/export_fab.py --board $(PCB) --config $(CONFIG)

# Two halves, and only the second one is dependency-free. check_placement
# reads the BOARD through pcbnew and can name what it found (`R12 pad 2`);
# validate_gerbers reads the exported bytes as text and cannot, but those are
# the bytes the fab will plot. A pass in the first and a fail in the second
# means the export moved something -- which is the whole point of having both.
check: check-placement parity
	@$(RUN)python3 scripts/validate_gerbers.py $(BOARD_DIR)/fab -c $(CONFIG) --kicad-netlist $(KICAD_NET)

# The schematic a reviewer opens IS the final board: KiCad's own parity,
# an independent eeschema-netlist-vs-pads diff, and sampled DRC for shorts
# and unconnected copper -- on the board after every post-route pass.
parity:
	$(RUN)$(P)/check_parity.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG)

# Freeze the reviewed project + the ordered package under
# $(BOARD_DIR)/release/rev-<[build] revision>/, with hashes. Commit it.
release:
	$(RUN)$(P)/release.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG)

# Hashes, zip, and (in the container) re-derive the package from the
# released board. RELEASE defaults to the highest rev-* directory.
verify-release:
	@test -n "$(RELEASE)" || { echo "no $(BOARD_DIR)/release/rev-* — run make release"; exit 1; }
	$(RUN)python3 scripts/verify_release.py $(RELEASE)

test-parity:
	$(RUN)$(P)/test_parity.py $(BOARD_DIR)

# Connector accessibility + copper in keepouts, straight off the board file.
# Runs warn-only inside `build` (where the board is not final) and hard here.
check-placement:
	@$(RUN)$(P)/check_placement.py --board $(PCB) --netlist $(NETLIST) --config $(CONFIG)

clean:
	rm -rf $(BOARD_DIR)/fab $(BOARD_DIR)/build
