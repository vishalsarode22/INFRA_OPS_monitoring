"""
First-run setup wizard (Tkinter -- ships with Python, no extra install).
Runs once when packaging/launcher.py finds no config/systems.yaml or .env
in the install folder. Writes both, then the launcher starts the dashboard.

Also checks for the external tools this app depends on but can't bundle
(SAP GUI, Tesseract OCR, a LaTeX engine for PDF generation) and warns
clearly rather than letting the app fail mysteriously later.
"""
import os
import shutil
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import yaml


def _check_prereqs() -> list[str]:
    """Returns a list of human-readable warnings for missing external tools."""
    warnings = []

    saplogon_candidates = [
        r"C:\Program Files (x86)\SAP\FrontEnd\SAPgui\saplogon.exe",
        r"C:\Program Files\SAP\FrontEnd\SAPgui\saplogon.exe",
    ]
    if not any(os.path.isfile(p) for p in saplogon_candidates):
        warnings.append(
            "SAP GUI (saplogon.exe) was not found in its usual install location. "
            "You can still continue setup and set the path manually, but SAP GUI "
            "must be installed on this machine for monitoring to work."
        )

    if shutil.which("pdflatex") is None:
        warnings.append(
            "pdflatex was not found on PATH. Install a LaTeX distribution "
            "(e.g. MiKTeX, free at miktex.org) for PDF report generation to work."
        )

    if shutil.which("tesseract") is None:
        warnings.append(
            "Tesseract OCR was not found on PATH. Install it (e.g. from "
            "github.com/UB-Mannheim/tesseract/wiki) for screenshot text "
            "extraction to work -- monitoring will still run without it, "
            "just with less detail in some fields."
        )

    return warnings


class SystemFrame(ttk.LabelFrame):
    """One repeatable block of fields for a single SAP system."""

    def __init__(self, parent, index: int, on_remove):
        super().__init__(parent, text=f"System {index}", padding=10)
        self.on_remove = on_remove

        self.vars = {
            "name": tk.StringVar(),
            "connection_name": tk.StringVar(),
            "client": tk.StringVar(),
            "username": tk.StringVar(),
            "password": tk.StringVar(),
            "language": tk.StringVar(value="EN"),
            "has_gui_access": tk.BooleanVar(value=True),
            "has_os_access": tk.BooleanVar(value=False),
            "ssh_host": tk.StringVar(),
            "ssh_port": tk.StringVar(value="22"),
            "ssh_username": tk.StringVar(),
            "ssh_password": tk.StringVar(),
            "sap_instance_nr": tk.StringVar(),
        }

        row = 0
        self._field("System name (e.g. TST, or an IP)", "name", row); row += 1
        self._field("SAP Logon connection name", "connection_name", row); row += 1
        self._field("Client", "client", row); row += 1
        self._field("Username", "username", row); row += 1
        self._field("Password", "password", row, show="*"); row += 1
        self._field("Language", "language", row); row += 1

        ttk.Checkbutton(self, text="Has SAP GUI access", variable=self.vars["has_gui_access"]).grid(
            row=row, column=0, sticky="w", pady=2); row += 1
        os_check = ttk.Checkbutton(
            self, text="Has OS/SSH access (Linux + SAP process metrics)",
            variable=self.vars["has_os_access"], command=self._toggle_ssh_fields
        )
        os_check.grid(row=row, column=0, sticky="w", pady=2); row += 1

        self.ssh_frame = ttk.Frame(self)
        self.ssh_frame.grid(row=row, column=0, columnspan=2, sticky="ew"); row += 1
        srow = 0
        self._ssh_field("SSH host", "ssh_host", srow); srow += 1
        self._ssh_field("SSH port", "ssh_port", srow); srow += 1
        self._ssh_field("SSH username", "ssh_username", srow); srow += 1
        self._ssh_field("SSH password", "ssh_password", srow, show="*"); srow += 1
        self._ssh_field("SAP instance number (e.g. 02)", "sap_instance_nr", srow); srow += 1
        self._toggle_ssh_fields()

        ttk.Button(self, text="Remove this system", command=lambda: self.on_remove(self)).grid(
            row=row, column=0, sticky="w", pady=(8, 0))

    def _field(self, label, key, row, show=None):
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(self, textvariable=self.vars[key], width=40, show=show or "").grid(
            row=row, column=1, sticky="w", padx=(8, 0), pady=2)

    def _ssh_field(self, label, key, row, show=None):
        ttk.Label(self.ssh_frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(self.ssh_frame, textvariable=self.vars[key], width=40, show=show or "").grid(
            row=row, column=1, sticky="w", padx=(8, 0), pady=2)

    def _toggle_ssh_fields(self):
        state = "normal" if self.vars["has_os_access"].get() else "disabled"
        for child in self.ssh_frame.winfo_children():
            if isinstance(child, ttk.Entry):
                child.configure(state=state)

    def to_dict(self) -> dict:
        d = {
            "name": self.vars["name"].get().strip(),
            "connection_name": self.vars["connection_name"].get().strip(),
            "client": self.vars["client"].get().strip(),
            "username": self.vars["username"].get().strip(),
            "password": self.vars["password"].get(),
            "language": self.vars["language"].get().strip() or "EN",
            "has_gui_access": self.vars["has_gui_access"].get(),
            "has_os_access": self.vars["has_os_access"].get(),
        }
        if d["has_os_access"]:
            d["ssh_host"] = self.vars["ssh_host"].get().strip()
            d["ssh_port"] = int(self.vars["ssh_port"].get().strip() or 22)
            d["ssh_username"] = self.vars["ssh_username"].get().strip()
            d["ssh_password"] = self.vars["ssh_password"].get()
            d["sap_instance_nr"] = self.vars["sap_instance_nr"].get().strip()
        return d


class WizardApp:
    def __init__(self, root: tk.Tk, install_dir: str):
        self.root = root
        self.install_dir = install_dir
        self.completed = False
        root.title("SAP BASIS Monitor -- First-Time Setup")
        root.geometry("620x700")

        outer = ttk.Frame(root, padding=10)
        outer.pack(fill="both", expand=True)

        warnings = _check_prereqs()
        if warnings:
            warn_box = tk.Text(outer, height=5, wrap="word", bg="#fff3cd")
            warn_box.insert("1.0", "\u26a0 " + "\n\n\u26a0 ".join(warnings))
            warn_box.configure(state="disabled")
            warn_box.pack(fill="x", pady=(0, 10))

        # --- SAP Logon path ---
        launch_frame = ttk.LabelFrame(outer, text="SAP Logon", padding=10)
        launch_frame.pack(fill="x", pady=(0, 10))
        self.saplogon_path = tk.StringVar(
            value=r"C:\Program Files (x86)\SAP\FrontEnd\SAPgui\saplogon.exe"
        )
        ttk.Label(launch_frame, text="saplogon.exe path").grid(row=0, column=0, sticky="w")
        ttk.Entry(launch_frame, textvariable=self.saplogon_path, width=50).grid(
            row=0, column=1, padx=(8, 4))
        ttk.Button(launch_frame, text="Browse...", command=self._browse_saplogon).grid(
            row=0, column=2)

        # --- SMTP ---
        smtp_frame = ttk.LabelFrame(outer, text="Email alerts (SMTP)", padding=10)
        smtp_frame.pack(fill="x", pady=(0, 10))
        self.smtp_vars = {
            "host": tk.StringVar(), "port": tk.StringVar(value="587"),
            "username": tk.StringVar(), "password": tk.StringVar(),
            "from_email": tk.StringVar(), "to_emails": tk.StringVar(),
        }
        self._grid_field(smtp_frame, "SMTP host", self.smtp_vars["host"], 0)
        self._grid_field(smtp_frame, "SMTP port", self.smtp_vars["port"], 1)
        self._grid_field(smtp_frame, "SMTP username", self.smtp_vars["username"], 2)
        self._grid_field(smtp_frame, "SMTP password", self.smtp_vars["password"], 3, show="*")
        self._grid_field(smtp_frame, "From email", self.smtp_vars["from_email"], 4)
        self._grid_field(smtp_frame, "Alert recipients (comma-separated)", self.smtp_vars["to_emails"], 5)

        # --- AI ---
        ai_frame = ttk.LabelFrame(outer, text="AI analysis (optional)", padding=10)
        ai_frame.pack(fill="x", pady=(0, 10))
        self.gemini_key = tk.StringVar()
        self.use_mock_ai = tk.BooleanVar(value=False)
        ttk.Label(ai_frame, text="Gemini API key (leave blank to use mock analysis)").grid(
            row=0, column=0, sticky="w")
        ttk.Entry(ai_frame, textvariable=self.gemini_key, width=40, show="*").grid(
            row=0, column=1, padx=(8, 0))

        # --- Systems (scrollable) ---
        systems_label_frame = ttk.LabelFrame(outer, text="SAP Systems", padding=10)
        systems_label_frame.pack(fill="both", expand=True, pady=(0, 10))

        canvas = tk.Canvas(systems_label_frame, height=250)
        scrollbar = ttk.Scrollbar(systems_label_frame, orient="vertical", command=canvas.yview)
        self.systems_container = ttk.Frame(canvas)
        self.systems_container.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self.systems_container, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.system_frames: list[SystemFrame] = []
        ttk.Button(outer, text="+ Add another system", command=self._add_system).pack(pady=(0, 10))
        self._add_system()  # start with one

        # --- Finish ---
        ttk.Button(outer, text="Save and Start Monitoring", command=self._finish).pack(pady=(0, 10))

    def _grid_field(self, parent, label, var, row, show=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=var, width=40, show=show or "").grid(
            row=row, column=1, sticky="w", padx=(8, 0), pady=2)

    def _browse_saplogon(self):
        path = filedialog.askopenfilename(
            title="Locate saplogon.exe", filetypes=[("Executable", "*.exe")]
        )
        if path:
            self.saplogon_path.set(path)

    def _add_system(self):
        frame = SystemFrame(self.systems_container, len(self.system_frames) + 1, self._remove_system)
        frame.pack(fill="x", pady=5, padx=5)
        self.system_frames.append(frame)

    def _remove_system(self, frame: SystemFrame):
        if len(self.system_frames) <= 1:
            messagebox.showwarning("Can't remove", "At least one system is required.")
            return
        frame.destroy()
        self.system_frames.remove(frame)

    def _finish(self):
        systems = [f.to_dict() for f in self.system_frames]
        for s in systems:
            if not s["name"] or not s["connection_name"]:
                messagebox.showerror("Missing info", "Every system needs a name and connection name.")
                return

        if not self.saplogon_path.get().strip():
            messagebox.showerror("Missing info", "SAP Logon path is required.")
            return

        # --- Write config/systems.yaml ---
        config_dir = os.path.join(self.install_dir, "config")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "systems.yaml"), "w", encoding="utf-8") as f:
            yaml.safe_dump({"systems": systems}, f, default_flow_style=False, sort_keys=False)

        # --- Write .env ---
        first = systems[0]
        smtp = self.smtp_vars
        env_lines = [
            f"SAPLOGON_EXE_PATH={self.saplogon_path.get().strip()}",
            f"SAP_CONNECTION_NAME={first['connection_name']}",
            f"SAP_CLIENT={first['client']}",
            f"SAP_USERNAME={first['username']}",
            f"SAP_PASSWORD={first['password']}",
            f"SAP_LANGUAGE={first['language']}",
            f"SMTP_HOST={smtp['host'].get().strip()}",
            f"SMTP_PORT={smtp['port'].get().strip() or '587'}",
            f"SMTP_USERNAME={smtp['username'].get().strip()}",
            f"SMTP_PASSWORD={smtp['password'].get()}",
            f"ALERT_FROM_EMAIL={smtp['from_email'].get().strip()}",
            f"ALERT_TO_EMAILS={smtp['to_emails'].get().strip()}",
            f"GEMINI_API_KEY={self.gemini_key.get().strip()}",
            f"USE_MOCK_AI={'true' if not self.gemini_key.get().strip() else 'false'}",
        ]
        if first.get("has_os_access"):
            env_lines += [
                f"SSH_HOST={first.get('ssh_host', '')}",
                f"SSH_PORT={first.get('ssh_port', 22)}",
                f"SSH_USERNAME={first.get('ssh_username', '')}",
                f"SSH_PASSWORD={first.get('ssh_password', '')}",
                f"SAP_INSTANCE_NR={first.get('sap_instance_nr', '')}",
            ]
        with open(os.path.join(self.install_dir, ".env"), "w", encoding="utf-8") as f:
            f.write("\n".join(env_lines) + "\n")

        self.completed = True
        self.root.destroy()


def run_wizard(install_dir: str) -> bool:
    """Returns True if the user completed setup, False if they cancelled."""
    root = tk.Tk()
    app = WizardApp(root, install_dir)
    root.mainloop()
    return app.completed
