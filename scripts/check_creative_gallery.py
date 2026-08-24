"""The creative gallery widget: one extension, escaped captions, eager images.

Three things this got wrong before it worked, each found by running it rather
than reading it:

  * Registering a second Apps() raised "Extension
    'io.modelcontextprotocol/ui' is already registered" and the server refused
    to start -- which in production is a container that fails to boot on
    PORT=8080 partway through a deploy, with every tool in it perfectly fine.
  * A revised prompt goes from the image model straight into the page. It is
    untrusted text and must be escaped, not interpolated.
  * loading="lazy" left every card below the fold blank until scrolled, which
    in a chat reads as broken rather than as pending.

The rendering itself is JavaScript and is verified in a browser; what is
checkable here is the shape that made those three failures possible.
"""
from __future__ import annotations

import os
import pathlib
import sys
import warnings

warnings.filterwarnings("ignore")
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JWT_SECRET_KEY", "ci-check-secret")
os.environ.setdefault("DATABASE_URL", "sqlite:///./check_gallery.db")

FAILURES: list[str] = []


def record(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -> ' + detail) if (detail and not ok) else ''}")
    if not ok:
        FAILURES.append(f"{label}: {detail}")


def main() -> int:
    import creative_app
    from mcp.server.apps import Apps

    html = creative_app.gallery_html()

    print("only one UI extension is ever registered:")
    source = (ROOT / "creative_app.py").read_text(encoding="utf-8")
    record("the gallery extends an Apps it is handed",
           "def register_creative_gallery(apps: Apps)" in source,
           "building a second Apps() stops the server booting at all")
    record("it does not construct its own", "Apps()" not in source,
           "'Extension is already registered' -- the container never starts")
    instance = (ROOT / "mcp_instance.py").read_text(encoding="utf-8")
    record("the server passes one extension carrying both",
           "register_creative_gallery(build_media_apps())" in instance)

    print("")
    print("model output cannot inject markup into the page:")
    record("captions go through an escape function", "function esc(" in html)
    record("it escapes the four that matter",
           all(s in html for s in ('"&amp;"', '"&lt;"', '"&gt;"', '"&quot;"')))
    record("the caption is escaped where it is used", "esc(img.revised_prompt" in html)
    record("the url is escaped too", "esc(img.url" in html)

    print("")
    print("the images actually appear:")
    record("nothing is lazy-loaded", 'loading="lazy"' not in html,
           "cards below the fold stay blank, which reads as broken in a chat")
    record("both payload shapes are handled",
           "window.openai" in html and "openai:set_globals" in html,
           "hosts differ; handling one renders nothing on the other")

    print("")
    print("a host with no widget is no worse off than before:")
    record("the payload carries plain links",
           "say_to_user_no_widget" in source)
    record("the tool tells the model to pass them on",
           "pass those on" in source or "Pass those on" in source)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("creative gallery: one extension, escaped captions, images that load")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
