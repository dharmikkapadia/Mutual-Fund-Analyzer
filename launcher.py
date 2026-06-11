"""
launcher.py — AFP desktop launcher for the NAV Explorer.

A thin customtkinter shell so the tool lives in the AFP suite and opens like a
desktop app. It starts Streamlit in headless mode via the *same* interpreter
(`python -m streamlit run app.py`) — the reliable invocation on the Store
build of Python where the Scripts dir isn't on PATH — and opens the browser.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

import customtkinter as ctk

BG, PANEL, TEXT, MUTED, ACCENT = ("#000000", "#101014", "#F2F2F5",
                                  "#9D9DA8", "#2962FF")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8501


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


class Launcher(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.proc = None
        self.title("AFP NAV Explorer")
        self.geometry("440x300")
        ctk.set_appearance_mode("dark")
        self.configure(fg_color=BG)

        ctk.CTkLabel(self, text="ARTHASHASTRA FINSEC",
                     text_color=MUTED,
                     font=ctk.CTkFont(size=11, weight="bold")).pack(pady=(26, 0))
        ctk.CTkLabel(self, text="NAV Explorer", text_color=TEXT,
                     font=ctk.CTkFont(size=26, weight="bold")).pack(pady=(0, 4))
        ctk.CTkLabel(self, text="Mutual-fund NAV, returns & rolling analysis",
                     text_color=MUTED).pack(pady=(0, 18))

        self.start_btn = ctk.CTkButton(self, text="▶  Launch app",
                                       fg_color=ACCENT, hover_color="#1E53E5",
                                       text_color="#FFFFFF",
                                       height=42, command=self.start)
        self.start_btn.pack(pady=6, padx=40, fill="x")
        self.stop_btn = ctk.CTkButton(self, text="■  Stop", fg_color=PANEL,
                                      hover_color="#2A2E39", text_color=TEXT,
                                      border_color="#2A2E39", border_width=1,
                                      height=36,
                                      command=self.stop, state="disabled")
        self.stop_btn.pack(pady=6, padx=40, fill="x")

        self.status = ctk.CTkLabel(self, text="Idle.", text_color=MUTED)
        self.status.pack(pady=14)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        self.status.configure(text="Starting server…")
        self.start_btn.configure(state="disabled")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "streamlit", "run", "app.py",
             "--server.headless", "true", "--server.port", str(PORT),
             "--browser.gatherUsageStats", "false"],
            cwd=APP_DIR)
        threading.Thread(target=self._wait_and_open, daemon=True).start()

    def _wait_and_open(self):
        for _ in range(60):
            if _port_open(PORT):
                webbrowser.open(f"http://localhost:{PORT}")
                self.status.configure(text=f"Running at localhost:{PORT}")
                self.stop_btn.configure(state="normal")
                return
            time.sleep(0.5)
        self.status.configure(text="Server did not start — check the console.")
        self.start_btn.configure(state="normal")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.status.configure(text="Stopped.")
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def on_close(self):
        self.stop()
        self.destroy()


if __name__ == "__main__":
    Launcher().mainloop()
