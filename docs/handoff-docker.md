# Handoff: containerize the pcb-agent pipeline

Paste this into Claude Code from the root of the `pcb-agent` repo.

---

You're working in the `pcb-agent` repo. Read `README.md`, `CLAUDE.md`, and
`scripts/build/CONTRACT.md` first — they describe a KiCad build pipeline where
`netlist.csv` is the source of truth and the KiCad files are build outputs.

The goal: someone should be able to run the headless parts of this pipeline
(`build`, `route`, `check`) without installing KiCad, Java, and Freerouting by
hand. They will still install KiCad natively to *look* at the board — that
part isn't containerizable and shouldn't be.

Work through these in order. Verify each step before moving on; don't assume a
result you haven't seen.

## 1. Answer the open question

Does the official KiCad image ship the SWIG `pcbnew` Python module, or only
the `kicad-cli` binary?

```bash
docker run --rm kicad/kicad:9.0 python3 -c "import pcbnew; print(pcbnew.GetBuildVersion())"
```

If that fails, try locating the module and setting `PYTHONPATH` before
concluding it's absent — at least one person running Python bindings in a
container had to point `PYTHONPATH` at KiCad's bundled modules explicitly.

Report what you find. It decides step 2.

## 2. Write the Dockerfile

- **If `pcbnew` imports from `kicad/kicad:9.0`** — base on it. Smaller, and
  version-pinned by the KiCad project.
- **If it doesn't** — base on `debian:bookworm` and install KiCad 9 from apt,
  where the bindings land in system-wide dist-packages and any Python 3 on the
  system can import them.

Either way, add:

- A JRE (21 unless Freerouting's pinned release needs otherwise)
- The Freerouting jar at a fixed path, **pinned to a specific release**, with
  its checksum verified at build time
- `uv`
- A non-root user whose UID/GID can be set by build arg, so files written to
  the mounted volume aren't owned by root

Do **not** put a GUI, X11, or VNC in it. The container builds; the human
reviews on the host.

## 3. Wire it up

- `docker-compose.yml` (or a `run.sh`) mounting the project directory as a
  volume, working dir inside it
- `make setup` — builds the image; also fetches the pinned Freerouting jar to
  `tools/` for people running natively, checksum-verified
- `make build` / `make route` / `make check` — run inside the container when
  it exists, fall back to local otherwise
- Update `scripts/doctor.py`: if the image is present, report the container as
  satisfying kicad/java/freerouting, and **compare the host's `kicad-cli
  --version` against the container's**, warning on a major-version mismatch.
  Host and container drifting apart is the failure mode this design creates.

## 4. Prove it

Don't declare it done on a successful `docker build`. Show me:

- `python3 scripts/doctor.py` inside the container, exiting clean
- `import pcbnew` working as the non-root user
- Freerouting running a real DSN to completion, foreground, `-mt 1`
- `scripts/validate_gerbers.py` passing on a known fab package
- A file written from inside the container, owned by my host user, not root

## 5. Then check something for me

The SWIG `pcbnew` bindings are reportedly present in KiCad 9 and 10 but
removed in KiCad 11, replaced by an IPC API that needs a running KiCad
instance rather than operating on files directly. Confirm whether that's
accurate against current KiCad developer docs.

If it is, this whole pipeline has a deadline, and a headless container is
exactly the environment the replacement API doesn't serve. Write what you find
to `docs/kicad-11-migration.md` — findings and options, no code yet.

## Rules

Same as `CLAUDE.md`. In particular: don't trust a tool's report of its own
success, pin every version you introduce, and if something doesn't work say
so plainly rather than working around it quietly.
