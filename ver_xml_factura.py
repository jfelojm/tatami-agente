"""Lista XML en Drive y muestra un preview; delega en inspeccionar_xml_drive."""

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(
        str(Path(__file__).resolve().parent / "inspeccionar_xml_drive.py"),
        run_name="__main__",
    )
