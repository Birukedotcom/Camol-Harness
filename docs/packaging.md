# Packaging and release gates

The current development package is `0.3.0a1`. Alpha means the feature integration
and release gates remain open; it does not certify the full `SPEC.md`.

## Verified local installation

Build/install in a virtual environment. The base package has no third-party runtime
dependencies and opens the line-mode CLI. Install `[tui]` for the full windowed
terminal client. `[graph]` adds TOML parsing on Python versions before 3.11; newer
Python uses its standard parser. The package supports Python 3.9+ on POSIX hosts.
Native Windows is not claimed: supervisor locking, Unix sockets, process groups,
and PTY tests require POSIX. Windows users can investigate a Linux/WSL environment.

```text
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[tui,graph]'
.venv/bin/camol --version
.venv/bin/camol
```

`camol._version` is the version source for modern `pyproject.toml`, legacy
`setup.py`, and `camol --version`. Legacy setup metadata remains for older bundled
macOS pip; upgrading pip is still recommended. Wheels include every kernel module
and the text/profile boot assets. Source distributions additionally include the
specification, docs, examples, regression tests and proof scripts.

The local packaging check built a wheel in a disposable directory, installed it
with Python 3.12 outside the source checkout, imported `Harness`, verified bundled
camel art, and entered/exited the real line client. This does not yet prove every
OS/architecture installation path. The CI matrix runs Python 3.9/3.12/3.13 on Linux
and macOS, plus separate package builds and recovery soaks; its remote results must
be inspected before release.

## Homebrew development formula

`Formula/camol.rb` is a HEAD-only development formula, not a published stable tap.
It follows Homebrew's documented [Python application layout](https://docs.brew.sh/Language-Specific-Formulae)
and pins the full tested Textual dependency tree using source archive checksums.
The interpreter is the supported [Python 3.12 formula](https://formulae.brew.sh/formula/python@3.12).
`python scripts/verify_homebrew_resources.py` streams and hashes those ten source
archives without installing or executing them. Resource bytes and Ruby syntax
have been verified locally. Homebrew itself is not installed on the review host,
so actual `brew install`, `brew test` and audit remain explicit release gates.

A stable formula needs a reviewed source tag/archive checksum and a chosen project
license. No license has been inferred from a public repository or assigned on the
owner's behalf. A separate tap may be published only when requested/approved.

## Release checklist

- Run the complete suite and adversarial regressions on the exact commit to ship.
- Run the pinned long-test corpus; publish its fixture-vs-live claim boundaries.
- Build wheel and sdist, inspect their file lists, and install both outside the repo.
- Exercise TUI boot, slash palette, login handoff, prompt, Ctrl+C and reattachment.
- Confirm no credentials, private fixtures, local state or environment snapshots are
  present in the package or Git history being released.
- Run the Homebrew install/test/audit on a disposable supported host.
- Resolve the project license with its owner; preserve the requested Git identity.
- Complete explicitly authorized, capped live-provider checks without claiming
  unsupported cost/egress/model guarantees.
- Resolve remaining implementation acceptance rows before naming a full-spec release.
- Publish a tag/package/tap only with release authority; a successful build alone
  is not publication authorization.
