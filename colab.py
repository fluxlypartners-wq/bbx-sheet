"""
colab.py — one-cell Colab launcher.

Sofascore blocks most datacenter IPs, so GitHub Actions often returns zero
odds. Google's IPs usually work. Paste this into a Colab cell:

    !pip -q install requests numpy
    %cd /content/drive/MyDrive/bbx
    import colab; colab.go()

Mount Drive first so picks.csv survives between sessions:

    from google.colab import drive; drive.mount('/content/drive')
"""

import os
import sys


def go(leagues="MLB,NPB,KBO,LMB", date=None, sims=40000, history=45):
    argv = ["--leagues", leagues, "--sims", str(sims), "--history", str(history)]
    if date:
        argv += ["--date", date]
    sys.path.insert(0, os.getcwd())
    import run
    return run.main(argv)


def dry():
    sys.path.insert(0, os.getcwd())
    import run
    return run.main(["--dry"])
