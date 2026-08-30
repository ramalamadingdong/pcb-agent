.PHONY: doctor build route check clean

doctor:
	@python3 scripts/doctor.py

check:
	@python3 scripts/validate_gerbers.py ./fab/ -c board.toml

# --- these need the build passes dropped into scripts/build/ ---
# See scripts/build/CONTRACT.md for the interface each one must satisfy.

build:
	@echo "Build passes not installed. See scripts/build/CONTRACT.md" && exit 1

route:
	@echo "Route script not installed. See scripts/build/CONTRACT.md" && exit 1

clean:
	rm -rf fab/ build/
