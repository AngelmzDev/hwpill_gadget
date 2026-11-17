# hwpill_gadget — mini-panel flotante con uso de CPU/GPU/RAM, temperatura y VRAM de GPU + métricas opcionales
# Requiere: psutil (obligatorio); nvidia-ml-py (GPU NVIDIA) y pythonnet + lib\*.dll (potencia y reloj de CPU), ambos opcionales.
# La potencia y el reloj de CPU solo funcionan con administrador y con las DLL de LibreHardwareMonitor (ver sensores.py).

import atexit
import ctypes
import glob
import json
import logging
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from logging.handlers import RotatingFileHandler

import psutil

import sensores

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
LOG_PATH = os.path.join(BASE, "gadget.log")
CREATE_NO_WINDOW = 0x08000000

logging.basicConfig(
    handlers=[RotatingFileHandler(LOG_PATH, maxBytes=200_000, backupCount=1, encoding="utf-8")],
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("gadget")


# ─── Configuración ────────────────────────────────────────────────────────────

DEFAULT_METRICS = ["cpu", "gpu", "ram"]
DEFAULT_ALPHA = 0.88


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(**changes):
    cfg = load_config()
    cfg.update(changes)
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except Exception:
        log.exception("No se pudo guardar config.json")


# ─── Sensores ─────────────────────────────────────────────────────────────────

def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def pawnio_installed():
    try:
        psutil.win_service_get("PawnIO")
        return True
    except Exception:
        return os.path.exists(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "PawnIO"))


class LhmReader:
    """CPU vía LibreHardwareMonitor: temperatura, potencia y reloj (necesita admin)."""

    def __init__(self):
        self.computer = None
        self.SensorType = None
        self.status = "ok"  # ok | sin_admin | error_lib | sin_sensor
        if not is_admin():
            self.status = "sin_admin"
            return
        if not sensores.instaladas(os.path.join(BASE, "lib")):
            self.status = "sin_lib"
            return
        try:
            lib = os.path.join(BASE, "lib")
            sys.path.append(lib)
            import clr
            # Precarga las dependencias (System.Memory, System.Management…) además de la librería
            for dll in sorted(glob.glob(os.path.join(lib, "*.dll"))):
                try:
                    clr.AddReference(dll)
                except Exception:
                    log.warning("No se pudo precargar %s", os.path.basename(dll), exc_info=True)
            from LibreHardwareMonitor import Hardware
            self.SensorType = Hardware.SensorType
            computer = Hardware.Computer()
            computer.IsCpuEnabled = True
            computer.Open()
            self.computer = computer
        except Exception:
            self.status = "error_lib"
            self.computer = None
            log.exception("LibreHardwareMonitor no se pudo inicializar")

    def read(self):
        """dict(temp, power, clock) con None donde no haya dato; None si no hay lector."""
        if not self.computer:
            return None
        try:
            T = self.SensorType
            temp = fallback = power = None
            clocks = []
            for hw in self.computer.Hardware:
                hw.Update()
                for sensor in hw.Sensors:
                    if sensor.Value is None:
                        continue
                    value = float(sensor.Value)
                    name = str(sensor.Name)
                    kind = sensor.SensorType
                    if kind == T.Temperature:
                        # Intel: "CPU Package" / AMD: "Core (Tctl/Tdie)"
                        if "Package" in name or "Tctl" in name or "Tdie" in name:
                            temp = value
                        fallback = value if fallback is None else max(fallback, value)
                    elif kind == T.Power and "Package" in name:
                        power = value
                    elif kind == T.Clock and name.startswith("Core #"):
                        clocks.append(value)
            temp = temp if temp is not None else fallback
            if temp is None and self.status == "ok":
                self.status = "sin_sensor"
                log.warning("LibreHardwareMonitor abierto pero sin sensor de temperatura de CPU "
                            "(PawnIO instalado: %s)", pawnio_installed())
            elif temp is not None:
                self.status = "ok"
            return {"temp": temp, "power": power,
                    "clock": sum(clocks) / len(clocks) if clocks else None}
        except Exception:
            log.exception("Error leyendo sensores de CPU")
            return None

    def close(self):
        if self.computer:
            try:
                self.computer.Close()
            except Exception:
                log.exception("Error cerrando LibreHardwareMonitor")
            self.computer = None

    def status_text(self):
        if self.status == "sin_lib":
            return "potencia y reloj de CPU: sensores no instalados (menú ⋯ → Instalar sensores de CPU)"
        if self.status == "sin_admin":
            return "potencia y reloj de CPU requieren ejecutar como administrador"
        if self.status == "error_lib":
            return "temp CPU: no se pudo cargar el sensor (ver gadget.log)"
        if self.status == "sin_sensor":
            if not pawnio_installed():
                return "temp CPU: falta el driver PawnIO (winget install namazso.PawnIO)"
            return "temp CPU: sin sensor en esta CPU"
        return ""


class GpuReader:
    """GPU NVIDIA vía NVML (nvidia-ml-py); si no está disponible, vía nvidia-smi."""

    def __init__(self):
        self.nvml = None
        self.handle = None
        try:
            import pynvml
            pynvml.nvmlInit()
            self.handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self.nvml = pynvml
        except Exception:
            log.warning("NVML no disponible, se usará nvidia-smi", exc_info=True)

    def read(self):
        """dict(pct, temp, vram_pct, vram_used_gb, power) con None donde falle."""
        if self.handle is not None:
            try:
                return self._read_nvml()
            except Exception:
                log.exception("Fallo NVML, probando nvidia-smi")
        return self._read_smi()

    def _read_nvml(self):
        n, h = self.nvml, self.handle
        out = {"pct": None, "temp": None, "vram_pct": None, "vram_used_gb": None, "power": None}
        out["pct"] = n.nvmlDeviceGetUtilizationRates(h).gpu
        out["temp"] = n.nvmlDeviceGetTemperature(h, n.NVML_TEMPERATURE_GPU)
        try:
            mem = n.nvmlDeviceGetMemoryInfo(h)
            out["vram_pct"] = mem.used * 100 / mem.total
            out["vram_used_gb"] = mem.used / 1024**3
        except Exception:
            pass
        try:
            out["power"] = n.nvmlDeviceGetPowerUsage(h) / 1000
        except Exception:
            pass
        return out

    def _read_smi(self):
        out = {"pct": None, "temp": None, "vram_pct": None, "vram_used_gb": None, "power": None}
        try:
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5, creationflags=CREATE_NO_WINDOW,
            )
            p = [x.strip() for x in res.stdout.strip().split(",")]

            def num(i):
                try:
                    return float(p[i])
                except Exception:
                    return None
            out["pct"], out["temp"] = num(0), num(1)
            used, total = num(2), num(3)
            if used is not None and total:
                out["vram_pct"] = used * 100 / total
                out["vram_used_gb"] = used / 1024
            out["power"] = num(4)
        except Exception:
            log.warning("nvidia-smi falló", exc_info=True)
        return out

    def close(self):
        if self.nvml:
            try:
                self.nvml.nvmlShutdown()
            except Exception:
                pass
            self.nvml = None
            self.handle = None


class Sensors:
    """Lee todo en un hilo aparte para no congelar la UI. close() lo detiene y libera los drivers."""

    def __init__(self):
        self.cpu_pct = self.cpu_temp = self.cpu_power = self.cpu_clock = None
        self.gpu_pct = self.gpu_temp = self.gpu_power = None
        self.vram_pct = self.vram_used_gb = None
        self.ram_pct = None
        self.ram_used_gb = self.ram_total_gb = 0.0
        self.net_down = self.net_up = self.disk_rate = None
        self._lhm = LhmReader()
        self._gpu = GpuReader()
        self._stop = threading.Event()
        self._closed = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    @property
    def cpu_temp_status(self):
        return self._lhm.status_text()

    def _loop(self):
        psutil.cpu_percent(interval=None)  # primera llamada: solo fija la referencia
        net0, disk0, t0 = psutil.net_io_counters(), psutil.disk_io_counters(), time.monotonic()
        try:
            while not self._stop.wait(1.0):
                try:
                    self.cpu_pct = psutil.cpu_percent(interval=None)
                    mem = psutil.virtual_memory()
                    self.ram_pct = mem.percent
                    self.ram_used_gb = (mem.total - mem.available) / 1024**3
                    self.ram_total_gb = mem.total / 1024**3

                    g = self._gpu.read()
                    self.gpu_pct, self.gpu_temp, self.gpu_power = g["pct"], g["temp"], g["power"]
                    self.vram_pct, self.vram_used_gb = g["vram_pct"], g["vram_used_gb"]

                    c = self._lhm.read()
                    if c:
                        self.cpu_temp, self.cpu_power, self.cpu_clock = c["temp"], c["power"], c["clock"]
                    else:
                        self.cpu_temp = self.cpu_power = self.cpu_clock = None

                    net1, disk1, t1 = psutil.net_io_counters(), psutil.disk_io_counters(), time.monotonic()
                    dt = max(t1 - t0, 1e-3)
                    self.net_down = (net1.bytes_recv - net0.bytes_recv) / dt
                    self.net_up = (net1.bytes_sent - net0.bytes_sent) / dt
                    if disk0 and disk1:
                        self.disk_rate = ((disk1.read_bytes + disk1.write_bytes)
                                          - (disk0.read_bytes + disk0.write_bytes)) / dt
                    net0, disk0, t0 = net1, disk1, t1
                except Exception:
                    log.exception("Error en el ciclo de lectura de sensores")
        finally:
            # Liberar drivers desde el propio hilo evita cerrar a mitad de una lectura
            self._lhm.close()
            self._gpu.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._thread.join(timeout=3)


# ─── UI ───────────────────────────────────────────────────────────────────────

BG = "#141414"
BG_MENU = "#1c1c1c"
FG_LABEL = "#8a8a8a"
FG_OK = "#e8e8e8"
FG_DIM = "#4a4a4a"      # botón del menú en reposo (sutil)
FG_BRIGHT = "#c8c8c8"   # botón del menú al pasar el ratón

# clave → (etiqueta del panel, ancho en caracteres de cada valor, texto del menú)
METRICS = {
    "cpu":  ("CPU",  (4,),   "CPU (uso)"),
    "gpu":  ("GPU",  (4, 4), "GPU (uso y temp)"),
    "ram":  ("RAM",  (4,),   "RAM"),
    "vram": ("VRAM", (4,),   "VRAM"),
    "cpuw": ("CPU",  (4,),   "Potencia CPU (W)"),
    "gpuw": ("GPU",  (4,),   "Potencia GPU (W)"),
    "clk":  ("CLK",  (6,),   "Reloj CPU"),
    "net":  ("NET",  (6, 6), "Red (↓ ↑)"),
    "dsk":  ("DSK",  (6,),   "Disco (lect+escr)"),
}
ORDER = ["cpu", "gpu", "ram", "vram", "cpuw", "gpuw", "clk", "net", "dsk"]
ALPHAS = (100, 88, 75, 60, 40)


def color_gradient(value, lo, hi):
    """Degradado continuo verde → amarillo → rojo entre lo y hi."""
    if value is None:
        return FG_LABEL
    t = max(0.0, min(1.0, (value - lo) / (hi - lo)))
    if t < 0.5:  # verde → amarillo
        r, g = int(510 * t), 220
    else:        # amarillo → rojo
        r, g = 255, int(220 * (1 - t) * 2)
    return f"#{r:02x}{g:02x}3c"


def fmt(value, suffix):
    return f"{round(value)}{suffix}" if value is not None else f"--{suffix}"


def fmt_rate(bps):
    if bps is None:
        return "--"
    if bps >= 1024**2:
        return f"{bps / 1024**2:.1f}M"
    if bps >= 1024:
        return f"{bps / 1024:.0f}K"
    return f"{bps:.0f}B"


class Gadget:
    def __init__(self):
        cfg = load_config()
        metrics = [k for k in cfg.get("metrics", DEFAULT_METRICS) if k in METRICS]
        self.enabled = metrics or list(DEFAULT_METRICS)
        self.locked = bool(cfg.get("locked", False))
        self.topmost = bool(cfg.get("topmost", True))
        try:
            self.alpha = min(1.0, max(0.2, float(cfg.get("alpha", DEFAULT_ALPHA))))
        except Exception:
            self.alpha = DEFAULT_ALPHA

        self.sensors = Sensors()
        atexit.register(self.sensors.close)
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", self.topmost)
        self.root.attributes("-alpha", self.alpha)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

        self.frame = tk.Frame(self.root, bg=BG, padx=10, pady=4)
        self.frame.pack()

        self._tooltip = None
        self._drag = None
        self.cells = {}
        self.menu_btn = None
        self._var_locked = tk.BooleanVar(value=self.locked)
        self._var_topmost = tk.BooleanVar(value=self.topmost)
        self._var_alpha = tk.IntVar(value=round(self.alpha * 100))
        self._vars_metric = {k: tk.BooleanVar(value=k in self.enabled) for k in METRICS}

        self._build()
        self._place_window()
        self._tick()
        if is_admin() and not sensores.instaladas(os.path.join(BASE, "lib")) and not cfg.get("sensores_preguntado"):
            save_config(sensores_preguntado=True)  # se pregunta una sola vez; luego queda el menú
            self.root.after(1500, self._instalar_sensores)

    # ── construcción de la barra ──
    def _build(self):
        self._tooltip_hide(None)
        for w in self.frame.winfo_children():
            w.destroy()
        self.cells = {}
        font_label = ("Segoe UI", 9, "bold")
        font_value = ("Segoe UI", 10, "bold")

        first = True
        for key in ORDER:
            if key not in self.enabled:
                continue
            label, widths, _ = METRICS[key]
            if not first:
                tk.Label(self.frame, text=" ", bg=BG, font=("Segoe UI", 4)).pack(side="left", padx=4)
            first = False
            tk.Label(self.frame, text=label, bg=BG, fg=FG_LABEL, font=font_label).pack(side="left")
            parts = []
            for i, width in enumerate(widths):
                lbl = tk.Label(self.frame, text="--", bg=BG, fg=FG_OK, font=font_value,
                               width=width, anchor="e")
                lbl.pack(side="left", padx=(4, 0) if i == 0 else (2, 0))
                parts.append(lbl)
            self.cells[key] = parts

        # Botón del menú: discreto, se ilumina al pasar el ratón
        self.menu_btn = tk.Label(self.frame, text="⋯", bg=BG, fg=FG_DIM,
                                 font=("Segoe UI", 11, "bold"), cursor="hand2", padx=2)
        self.menu_btn.pack(side="left", padx=(8, 0))
        self.menu_btn.bind("<Enter>", lambda e: self.menu_btn.config(fg=FG_BRIGHT))
        self.menu_btn.bind("<Leave>", lambda e: self.menu_btn.config(fg=FG_DIM))
        self.menu_btn.bind("<Button-1>", self._open_menu_from_button)

        # Arrastre, menú contextual y tooltip en el resto del panel
        for w in [self.frame] + [c for c in self.frame.winfo_children() if c is not self.menu_btn]:
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)
            w.bind("<Button-3>", self._open_menu_from_click)
            w.bind("<Enter>", self._tooltip_show)
            w.bind("<Leave>", self._tooltip_hide)

    # ── posición ──
    def _place_window(self):
        self.root.update_idletasks()
        cfg = load_config()
        try:
            x, y = int(cfg["x"]), int(cfg["y"])
        except Exception:
            x = self.root.winfo_screenwidth() - 360
            y = self.root.winfo_screenheight() - 78  # justo encima de la barra de tareas
        self.root.geometry(f"+{x}+{y}")

    def _save_pos(self):
        save_config(x=self.root.winfo_x(), y=self.root.winfo_y())

    def _reset_pos(self):
        cfg = load_config()
        cfg.pop("x", None)
        cfg.pop("y", None)
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
        except Exception:
            log.exception("No se pudo reiniciar la posición")
        self._place_window()

    # ── arrastre ──
    def _drag_start(self, e):
        if self.locked:
            return
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        if self._drag:
            self.root.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _drag_end(self, _):
        if self._drag:
            self._save_pos()
        self._drag = None

    # ── menú desplegable ──
    def _make_menu(self):
        opts = dict(tearoff=0, bg=BG_MENU, fg="#cfcfcf", activebackground="#333333",
                    activeforeground="#ffffff", selectcolor="#cfcfcf", bd=0,
                    font=("Segoe UI", 9))
        menu = tk.Menu(self.root, **opts)

        sub_metrics = tk.Menu(menu, **opts)
        for key in ORDER:
            sub_metrics.add_checkbutton(label=METRICS[key][2], variable=self._vars_metric[key],
                                        command=lambda k=key: self._toggle_metric(k))
        menu.add_cascade(label="Métricas", menu=sub_metrics)

        sub_alpha = tk.Menu(menu, **opts)
        for pct in ALPHAS:
            sub_alpha.add_radiobutton(label=f"{pct}%", value=pct, variable=self._var_alpha,
                                      command=lambda p=pct: self._set_alpha(p))
        menu.add_cascade(label="Transparencia", menu=sub_alpha)

        menu.add_checkbutton(label="Siempre encima de todo", variable=self._var_topmost,
                             command=self._toggle_topmost)
        menu.add_checkbutton(label="Bloquear posición", variable=self._var_locked,
                             command=self._toggle_lock)
        menu.add_command(label="Fijar (bloqueado y siempre encima)", command=self._fijar)
        menu.add_command(label="Reiniciar posición", command=self._reset_pos)
        if not sensores.instaladas(os.path.join(BASE, "lib")):
            menu.add_command(label="Instalar sensores de CPU (potencia y reloj)…", command=self._instalar_sensores)
        menu.add_separator()
        menu.add_command(label="Cerrar", command=self._quit)
        return menu

    def _popup(self, x, y):
        menu = self._make_menu()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _open_menu_from_button(self, _):
        b = self.menu_btn
        self._popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def _open_menu_from_click(self, e):
        self._popup(e.x_root, e.y_root)

    def _toggle_metric(self, key):
        wanted = [k for k in ORDER if self._vars_metric[k].get()]
        if not wanted:  # siempre debe quedar al menos una métrica visible
            self._vars_metric[key].set(True)
            return
        self.enabled = wanted
        save_config(metrics=wanted)
        self._build()

    def _set_alpha(self, pct):
        self.alpha = pct / 100
        self.root.attributes("-alpha", self.alpha)
        save_config(alpha=self.alpha)

    def _toggle_lock(self):
        self.locked = bool(self._var_locked.get())
        save_config(locked=self.locked)

    def _toggle_topmost(self):
        self.topmost = bool(self._var_topmost.get())
        self.root.attributes("-topmost", self.topmost)
        save_config(topmost=self.topmost)
        self._raise()

    def _instalar_sensores(self):
        """Descarga con permiso LibreHardwareMonitor (MPL-2.0) de NuGet, verificada por hash. Opcional."""
        from tkinter import messagebox
        if not messagebox.askyesno("Sensores de CPU", sensores.AVISO, parent=self.root):
            return

        def trabajo():
            ok, msg = sensores.instalar(os.path.join(BASE, "lib"))
            texto = msg + ("\n\nCierra y vuelve a abrir el gadget." if ok else "")
            self.root.after(0, lambda: messagebox.showinfo("Sensores de CPU", texto, parent=self.root))
        threading.Thread(target=trabajo, daemon=True).start()

    def _fijar(self):
        """Atajo: queda bloqueado en su sitio y siempre encima."""
        self._var_locked.set(True)
        self._var_topmost.set(True)
        self._toggle_lock()
        self._toggle_topmost()

    def _raise(self):
        """Reafirma 'siempre encima' en Windows: sube el panel por encima de otras ventanas, incluidas
        otras 'siempre encima' que hayan aparecido después. No le roba el foco a nadie."""
        if not self.topmost:
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            # HWND_TOPMOST = -1; NOSIZE 0x1 | NOMOVE 0x2 | NOACTIVATE 0x10 | SHOWWINDOW 0x40
            user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040)
        except Exception:
            log.exception("No se pudo reafirmar siempre-encima")

    def _quit(self):
        self._tooltip_hide(None)
        self.sensors.close()  # detiene el hilo y libera LibreHardwareMonitor / NVML
        self.root.destroy()

    # ── tooltip con detalle ──
    def _tooltip_show(self, _):
        if self._tooltip:
            return
        s = self.sensors
        parts = [f"RAM: {s.ram_used_gb:.1f} / {s.ram_total_gb:.1f} GB"]
        if s.vram_used_gb is not None:
            parts.append(f"VRAM: {s.vram_used_gb:.1f} GB")
        tip = tk.Toplevel(self.root)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text="  ·  ".join(parts), bg="#2a2a2a", fg="#dddddd",
                 font=("Segoe UI", 9), padx=8, pady=3).pack()
        tip.geometry(f"+{self.root.winfo_x()}+{self.root.winfo_y() - 30}")
        self._tooltip = tip

    def _tooltip_hide(self, _):
        if self._tooltip:
            self._tooltip.destroy()
            self._tooltip = None

    # ── refresco ──
    def _values(self):
        s = self.sensors
        W = None  # sin color propio: usa FG_OK
        return {
            "cpu": [(fmt(s.cpu_pct, "%"), color_gradient(s.cpu_pct, 40, 100))],
            "gpu": [(fmt(s.gpu_pct, "%"), color_gradient(s.gpu_pct, 40, 100)),
                    (fmt(s.gpu_temp, "°"), color_gradient(s.gpu_temp, 40, 85))],
            "ram": [(fmt(s.ram_pct, "%"), color_gradient(s.ram_pct, 40, 100))],
            "vram": [(fmt(s.vram_pct, "%"), color_gradient(s.vram_pct, 40, 100))],
            "cpuw": [(fmt(s.cpu_power, "W"), W)],
            "gpuw": [(fmt(s.gpu_power, "W"), W)],
            "clk": [(f"{s.cpu_clock / 1000:.1f}GHz" if s.cpu_clock is not None else "--GHz", W)],
            "net": [("↓" + fmt_rate(s.net_down), W), ("↑" + fmt_rate(s.net_up), W)],
            "dsk": [(fmt_rate(s.disk_rate) + "/s", W)],
        }

    def _tick(self):
        values = self._values()
        for key, labels in self.cells.items():
            for lbl, (text, color) in zip(labels, values[key]):
                lbl.config(text=text, fg=color or FG_OK)
        self._raise()  # cada segundo, para seguir encima aunque otra ventana "siempre encima" lo tape
        self.root.after(1000, self._tick)

    def run(self):
        try:
            self.root.mainloop()
        finally:
            self.sensors.close()


if __name__ == "__main__":
    try:
        Gadget().run()
    except Exception:
        log.exception("El gadget terminó con un error")
        raise
