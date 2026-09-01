# SPDX-License-Identifier: GPL-3.0-or-later
"""Which VISA implementation is under test, and where it came from.

Every check in this suite is written against the VISA API, not against
pyvisa-py, so the same check can be pointed at any implementation. That is the
point: a check that fails on one backend and passes on another has found a
*disparity*, and a disparity is a stronger claim than a failure. It says the
behaviour is not merely undesirable but inconsistent with a shipping
implementation of the same spec, which is the argument that actually moves an
upstream discussion.

Backends are named by short id (`py`, `ni`, `rs`, `keysight`, `tek`, `sim`) or
by an explicit path to a VISA shared library. Only `py` and `sim` install with
pip; the rest are vendor packages a human installs from a GUI, so this module's
main job is to find them if they are there and to say plainly what is missing
if they are not. A backend that cannot be loaded is *skipped and reported*,
never silently dropped -- a comparison table with a column quietly absent reads
like agreement between the backends that remain.
"""

from __future__ import annotations

import dataclasses
import os
import platform
import subprocess
import sys
from pathlib import Path

# pyvisa is imported lazily, inside the two functions that need it. The table
# of which backends exist and where to get them is also read by the reporting
# tools, which render a page on a machine that never talks to an instrument --
# and making them install a VISA stack to look up a display name is the kind of
# dependency that turns a five-second job into a minute.


@dataclasses.dataclass(frozen=True)
class BackendSpec:
    """One VISA implementation this suite knows how to look for."""

    id: str
    name: str
    #: What pyvisa is handed: "@py", "@sim", or a path to a shared library.
    locator: str | None
    #: Candidate library paths per platform, first match wins.
    candidates: tuple[str, ...] = ()
    #: Where a human gets it, printed when it is missing.
    source: str = ""
    #: True for implementations that speak the real network protocols. A
    #: simulated backend answers the API without a socket, so it can only ever
    #: agree about API shape, never about wire behaviour.
    networked: bool = True


_SYSTEM = platform.system()


def _paths(darwin: tuple[str, ...] = (), linux: tuple[str, ...] = (),
           windows: tuple[str, ...] = ()) -> tuple[str, ...]:
    return {"Darwin": darwin, "Linux": linux, "Windows": windows}.get(_SYSTEM, ())


#: Every backend this suite knows about, in the order a report lists them.
BACKENDS: dict[str, BackendSpec] = {
    "py": BackendSpec(
        id="py",
        name="PyVISA-py",
        locator="@py",
        source="pip install pyvisa-py",
    ),
    "ni": BackendSpec(
        id="ni",
        name="NI-VISA",
        locator=None,
        candidates=_paths(
            darwin=("/Library/Frameworks/VISA.framework/VISA",),
            linux=(
                "/usr/lib/x86_64-linux-gnu/libvisa.so",
                "/usr/local/vxipnp/linux/lib64/libvisa.so",
                "/usr/lib/libvisa.so",
            ),
            windows=(r"C:\Windows\System32\visa64.dll", r"C:\Windows\System32\nivisa64.dll"),
        ),
        source="https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html",
    ),
    "rs": BackendSpec(
        id="rs",
        name="R&S VISA",
        locator=None,
        candidates=_paths(
            darwin=("/Library/Frameworks/RsVisa.framework/RsVisa",),
            linux=("/usr/lib/librsvisa.so", "/usr/local/lib/librsvisa.so"),
            windows=(r"C:\Windows\System32\RsVisa32.dll",),
        ),
        source="https://www.rohde-schwarz.com/applications/r-s-visa-application-note_56280-148812.html",
    ),
    "keysight": BackendSpec(
        id="keysight",
        name="Keysight IO Libraries",
        locator=None,
        candidates=_paths(
            # No macOS build exists; the tuple is empty there on purpose, so
            # the skip message names the platform rather than a missing file.
            linux=("/opt/keysight/iolibs/libktvisa32.so", "/usr/lib/libktvisa32.so"),
            windows=(r"C:\Windows\System32\ktvisa32.dll",),
        ),
        source="https://www.keysight.com/find/iosuite (Windows and Linux only)",
    ),
    "tek": BackendSpec(
        id="tek",
        name="TekVISA",
        locator=None,
        candidates=_paths(
            windows=(r"C:\Windows\System32\visa32.dll",),
        ),
        source="https://www.tek.com/en/support/software/driver/tekvisa-connectivity-software-v411 (Windows only)",
    ),
    "sim": BackendSpec(
        id="sim",
        name="PyVISA-sim",
        locator="@sim",
        source="pip install pyvisa-sim",
        networked=False,
    ),
}


@dataclasses.dataclass
class Resolved:
    """A backend that was asked for, and what became of the request."""

    spec: BackendSpec
    locator: str | None
    available: bool
    reason: str = ""

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def name(self) -> str:
        return self.spec.name

    def resource_manager(self) -> "pyvisa.ResourceManager":
        """The one ResourceManager for this backend.

        ``pyvisa.ResourceManager`` is a singleton *per backend*, so every
        caller with the same locator gets the same object. That matters for
        threads: ``ResourceManager.close()`` closes every session it owns, so
        a worker closing "its" manager tears down its siblings' sessions. This
        suite never closes a manager; sessions are closed individually.
        """
        if not self.available:
            raise RuntimeError(f"{self.name} is not available here: {self.reason}")
        import pyvisa

        return pyvisa.ResourceManager(self.locator)


class TreeError(RuntimeError):
    """A pyvisa-py tree was named that cannot be used."""


def use_pyvisa_py_tree(tree: str | os.PathLike) -> Path:
    """Put a pyvisa-py checkout ahead of whatever is installed.

    Comparing branches is the point of this suite, so pointing it at a tree
    checked out elsewhere has to be one obvious thing rather than a PYTHONPATH
    incantation the caller has to know wins over an editable install.

    Two rules here are load-bearing:

    - A tree that does not exist, or has no `pyvisa_py/` in it, is a hard
      error naming the path. Falling through to whatever happens to be
      installed would produce a run that looks fine and reports the wrong
      thing, and the provenance block would faithfully record a tree nobody
      asked for. Silently testing the wrong tree is worse than not running.

    - PYTHONPATH is set as well as `sys.path`, so subprocesses inherit it.
      `run_all.sh` spawns one process per script, and a tree that applied only
      to the parent would mean the suite runner and the scripts it launches
      disagreed about what was under test.
    """
    path = Path(tree).expanduser().resolve()
    if not path.exists():
        raise TreeError(f"no such pyvisa-py tree: {path}")
    if not (path / "pyvisa_py" / "__init__.py").exists():
        raise TreeError(
            f"{path} does not look like a pyvisa-py checkout "
            f"(no pyvisa_py/__init__.py in it)"
        )

    # Importing pyvisa_py before this point would pin the old module in
    # sys.modules and make the switch silently ineffective.
    already = sys.modules.get("pyvisa_py")
    if already is not None:
        loaded = Path(already.__file__).parent.parent
        if loaded != path:
            raise TreeError(
                f"pyvisa_py was already imported from {loaded}, so switching "
                f"to {path} would have no effect. Name the tree before any "
                f"import of it."
            )

    sys.path.insert(0, str(path))
    existing = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = f"{path}{os.pathsep}{existing}" if existing else str(path)
    return path


def resolve(spec_id: str) -> Resolved:
    """Find `spec_id`, reporting rather than raising when it is missing."""
    # An explicit path wins over any name, so an install in an unusual place
    # never needs this file edited.
    if "/" in spec_id or "\\" in spec_id or spec_id.endswith((".so", ".dll")):
        path = Path(spec_id)
        spec = BackendSpec(id=path.name, name=f"VISA at {spec_id}", locator=spec_id)
        if not path.exists():
            return Resolved(spec, None, False, f"no such file: {spec_id}")
        return Resolved(spec, spec_id, True)

    spec = BACKENDS.get(spec_id)
    if spec is None:
        known = ", ".join(BACKENDS)
        raise KeyError(f"unknown backend {spec_id!r}; known ids are {known}")

    if spec.locator is not None:
        # "@py" and "@sim" are python packages: importable or not.
        module = {"@py": "pyvisa_py", "@sim": "pyvisa_sim"}[spec.locator]
        try:
            __import__(module)
        except ImportError as exc:
            return Resolved(spec, None, False, f"{module} is not importable ({exc})")
        return Resolved(spec, spec.locator, True)

    if not spec.candidates:
        return Resolved(
            spec, None, False, f"no {spec.name} build exists for {_SYSTEM}"
        )

    for candidate in spec.candidates:
        if Path(candidate).exists():
            return Resolved(spec, candidate, True)

    looked = ", ".join(spec.candidates)
    return Resolved(spec, None, False, f"not installed (looked in {looked})")


def resolve_all(spec_ids: list[str]) -> list[Resolved]:
    return [resolve(s) for s in spec_ids]


def available_ids() -> list[str]:
    """Backend ids that can actually be loaded on this machine."""
    return [i for i in BACKENDS if resolve(i).available]


def _pyvisa_version() -> str:
    try:
        import pyvisa
    except ImportError as exc:  # pragma: no cover - a run always has it
        return f"not importable ({exc})"
    return pyvisa.__version__


def provenance(resolved: Resolved) -> dict[str, str]:
    """Where the implementation under test came from.

    A result is only reproducible if the thing that produced it can be named.
    For pyvisa-py that means the checkout and the commit, not just a version:
    the whole reason this suite exists is comparing branches of it, and two
    branches report the same ``__version__`` right up until one of them is
    released.
    """
    info: dict[str, str] = {
        "backend": resolved.name,
        "locator": resolved.locator or "(unavailable)",
        "pyvisa": _pyvisa_version(),
        "python": sys.version.split()[0],
        "platform": f"{_SYSTEM} {platform.release()}",
    }

    if resolved.locator == "@py":
        try:
            import pyvisa_py

            path = Path(pyvisa_py.__file__).parent
            # __version__ comes from installed distribution metadata, which is
            # stamped at install time and does not follow a tree put on
            # sys.path afterwards. Reporting it unqualified next to a
            # different checkout is how a run gets attributed to the wrong
            # commit; the git describe below is the authoritative line.
            info["pyvisa-py path"] = str(path)
            info["pyvisa-py commit"] = _git_describe(path.parent)
            info["pyvisa-py version"] = (
                f"{pyvisa_py.__version__} (installed metadata, "
                f"not necessarily this tree)"
            )
        except ImportError:
            pass
    return info


def _git_describe(tree: Path) -> str:
    """`git describe` for a checkout, or a note saying why not."""
    try:
        out = subprocess.run(
            ["git", "-C", str(tree), "describe", "--always", "--dirty", "--tags"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"(git failed: {exc})"
    if out.returncode != 0:
        return "(not a git checkout)"

    described = out.stdout.strip()
    branch = subprocess.run(
        ["git", "-C", str(tree), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()
    return f"{described} on {branch}" if branch else described


def describe_environment(resolved: Resolved) -> str:
    lines = [f"{k}: {v}" for k, v in provenance(resolved).items()]
    return "\n".join(f"  {line}" for line in lines)


def pyvisa_py_tree_note(chosen: Path | None = None) -> str | None:
    """Warn when PYTHONPATH shadows the installed pyvisa-py.

    A checkout on PYTHONPATH takes precedence over an editable install, which
    is convenient and completely invisible in the output unless something says
    so. A result attributed to the wrong tree is worse than no result.

    An inherited PYTHONPATH is worth flagging; one this run set itself via
    --pyvisa-py is not, because the provenance block already names that tree
    and the commit it is on.
    """
    if chosen is not None:
        return None
    path = os.environ.get("PYTHONPATH")
    if not path:
        return None
    return f"PYTHONPATH is set, which takes precedence over any install: {path}"
