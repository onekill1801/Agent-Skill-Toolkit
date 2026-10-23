"""Console encoding (checklist R.M3): UTF-8 stdout / stderr so JSON with Vietnamese text never crashes on
a Windows cp1252 / cp437 console. Same effect as the reconfigure loop in the skills' config.py /
probe_common.py.

    from agent_skill_toolkit.core import console
    console.utf8()
"""

import sys


def utf8(streams=None) -> None:
    for stream in streams or (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
