"""The upload widget must parse, and must keep every way of handing over a file.

The widget is one hand-written HTML document served to a sandboxed iframe. A
syntax error in its script does not fail a request or appear in any log -- the
drop zone simply renders and does nothing, which is indistinguishable from the
host not mounting it. That failure mode has already cost this project a full
debugging session once (the CSP/CORS hunt), so the script is syntax-checked
here with node when it is available.

It also pins the three ways a file can arrive, because each covers a different
real situation:

  paste   the user has the image in the clipboard -- including right after
          pasting it into the chat and being told the model cannot read it
  drop    the file is on disk and visible
  picker  neither of the above, or a phone

Losing one silently narrows the feature without breaking anything visible.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import warnings

from cryptography.fernet import Fernet

warnings.filterwarnings("ignore")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./check_widget.db")
os.environ.setdefault("JWT_SECRET_KEY", "ci-check-secret")
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())

FAILURES: list[str] = []


def record(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -> ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(f"{label}: {detail}")


def main() -> int:
    from media_upload import widget_html

    html = widget_html()

    print("the ways a file can be handed over:")
    for label, needle in (
        ("paste (Ctrl+V)", "addEventListener('paste'"),
        ("drag and drop", "addEventListener('drop'"),
        ("file picker", "F.addEventListener('change'"),
    ):
        record(label, needle in html)

    print("\nwhat the user is told:")
    record("the hint mentions pasting", "Ctrl+V" in html)

    print("\nthe script parses:")
    if "<script>" not in html:
        record("a script block exists", False, "no <script> in the widget")
    else:
        js = html.split("<script>")[1].split("</script>")[0]
        node = shutil.which("node")
        if not node:
            print("  SKIP  node not available; syntax not checked")
        else:
            with tempfile.TemporaryDirectory() as tmp:
                path = pathlib.Path(tmp) / "widget.js"
                path.write_text(js, encoding="utf-8")
                proc = subprocess.run([node, "--check", str(path)],
                                      capture_output=True, text=True)
                record("node --check", proc.returncode == 0,
                       (proc.stderr or "").strip().splitlines()[0] if proc.returncode else
                       f"{len(js)} bytes")

    # The drop zone is worthless if the model never opens it. These are the two
    # places that decide whether it gets offered without being asked.
    print("\nthe model is told to offer it:")
    import slim_mcp

    rules = slim_mcp._build_instructions()
    record("server rules name media_upload_start", "media_upload_start" in rules)
    record("server rules say not to wait to be asked",
           "without being asked" in rules.lower() or "do not wait" in rules.lower())

    from media_app import build_media_apps  # noqa: F401  (import proves it still builds)
    import media_app

    doc = media_app.upload_start_payload.__doc__ or ""
    tool_doc = ""
    for line in (media_app.__file__,):
        tool_doc = pathlib.Path(line).read_text(encoding="utf-8")
    record("the tool description lists its triggers",
           "attaches or pastes" in tool_doc and "without being asked" in tool_doc.lower())
    record("the payload hands the model a line to say", '"say_to_user"' in tool_doc)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("upload widget: parses, all hand-over paths wired, and the model is told to offer it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
