import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    import updater

    if updater.apply():
        subprocess.Popen([sys.executable, str(ROOT / "app.pyw")], cwd=str(ROOT))
        raise SystemExit(0)
    import app

    app.main()
except SystemExit:
    raise
except Exception:
    import tkinter
    from tkinter import messagebox

    tkinter.Tk().withdraw()
    messagebox.showerror("Chest Counter", traceback.format_exc())
