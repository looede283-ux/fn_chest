import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    try:
        import app

        app.main()
    except SystemExit:
        raise
    except Exception:
        import tkinter
        from tkinter import messagebox

        tkinter.Tk().withdraw()
        messagebox.showerror("Chest Counter", traceback.format_exc())
