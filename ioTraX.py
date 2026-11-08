import tkinter as tk
import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox
import customtkinter as ctk
import psutil
import time
import threading
import csv
import os
import secrets
from collections import deque
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.animation as animation
from matplotlib.figure import Figure

try:
    import win32pdh
    _HAS_PDH = True
except ImportError:
    _HAS_PDH = False

# Settings
MAX_SAMPLES = 60  # Number of points on the X axis
REFRESH_INTERVAL = 1000 # ms

class IOMonitor:
    def __init__(self):
        self.history = {} # disk_name: {metric: deque}
        self.disks = list(psutil.disk_io_counters(perdisk=True).keys())
        self.last_counters = psutil.disk_io_counters(perdisk=True)
        self.last_time = time.time()
        self.running = True
        self.logging = False
        self.log_files = {}
        self.csv_writers = {}
        
        for disk in self.disks:
            self.history[disk] = {
                'read_speed': deque([0.0] * MAX_SAMPLES, maxlen=MAX_SAMPLES),
                'write_speed': deque([0.0] * MAX_SAMPLES, maxlen=MAX_SAMPLES),
                'read_latency': deque([0.0] * MAX_SAMPLES, maxlen=MAX_SAMPLES),
                'write_latency': deque([0.0] * MAX_SAMPLES, maxlen=MAX_SAMPLES),
                'read_size': deque([0.0] * MAX_SAMPLES, maxlen=MAX_SAMPLES),
                'write_size': deque([0.0] * MAX_SAMPLES, maxlen=MAX_SAMPLES),
                'iops': deque([0.0] * MAX_SAMPLES, maxlen=MAX_SAMPLES),
            }

        self.pdh_query = None
        self.pdh_read_counters = {}
        self.pdh_write_counters = {}
        self._setup_pdh()

    def _setup_pdh(self):
        if not _HAS_PDH:
            return
        try:
            instances = win32pdh.EnumObjectItems(
                None, None, "PhysicalDisk", win32pdh.PERF_DETAIL_WIZARD
            )[1]
            self.pdh_query = win32pdh.OpenQuery()
            for disk in self.disks:
                disk_num = ''.join(c for c in disk if c.isdigit())
                pdh_inst = next((i for i in instances if i.split(' ')[0] == disk_num), None)
                if pdh_inst is None:
                    continue
                self.pdh_read_counters[disk] = win32pdh.AddCounter(
                    self.pdh_query, f"\\PhysicalDisk({pdh_inst})\\Avg. Disk sec/Read"
                )
                self.pdh_write_counters[disk] = win32pdh.AddCounter(
                    self.pdh_query, f"\\PhysicalDisk({pdh_inst})\\Avg. Disk sec/Write"
                )
            win32pdh.CollectQueryData(self.pdh_query)  # prime the counters
        except Exception as e:
            print(f"PDH setup failed, latency will use psutil fallback: {e}")
            self.pdh_query = None

    def update(self):
        current_counters = psutil.disk_io_counters(perdisk=True)
        if current_counters is None:
            return
        current_time = time.time()
        elapsed = current_time - self.last_time
        if elapsed <= 0: return

        # Collect PDH latency snapshot once per cycle (covers all disks)
        pdh_latency = {}
        if self.pdh_query:
            try:
                win32pdh.CollectQueryData(self.pdh_query)
                for disk in self.pdh_read_counters:
                    _, rl_sec = win32pdh.GetFormattedCounterValue(
                        self.pdh_read_counters[disk], win32pdh.PDH_FMT_DOUBLE
                    )
                    _, wl_sec = win32pdh.GetFormattedCounterValue(
                        self.pdh_write_counters[disk], win32pdh.PDH_FMT_DOUBLE
                    )
                    pdh_latency[disk] = (rl_sec * 1000.0, wl_sec * 1000.0)  # sec -> ms
            except Exception:
                pass

        for disk, counters in current_counters.items():
            if disk in self.last_counters:
                prev = self.last_counters[disk]

                # Deltas
                read_bytes = counters.read_bytes - prev.read_bytes
                write_bytes = counters.write_bytes - prev.write_bytes
                read_count = counters.read_count - prev.read_count
                write_count = counters.write_count - prev.write_count

                # Calculations
                rs = (read_bytes / (1024 * 1024)) / elapsed
                ws = (write_bytes / (1024 * 1024)) / elapsed
                if disk in pdh_latency:
                    rl, wl = pdh_latency[disk]
                else:
                    read_time = counters.read_time - prev.read_time
                    write_time = counters.write_time - prev.write_time
                    rl = (read_time / read_count) if read_count > 0 else 0
                    wl = (write_time / write_count) if write_count > 0 else 0
                rz = (read_bytes / read_count / 1024) if read_count > 0 else 0
                wz = (write_bytes / write_count / 1024) if write_count > 0 else 0
                iops = (read_count + write_count) / elapsed
                
                if disk in self.history:
                    self.history[disk]['read_speed'].append(rs)
                    self.history[disk]['write_speed'].append(ws)
                    self.history[disk]['read_latency'].append(rl)
                    self.history[disk]['write_latency'].append(wl)
                    self.history[disk]['read_size'].append(rz)
                    self.history[disk]['write_size'].append(wz)
                    self.history[disk]['iops'].append(iops)

                    if self.logging and disk in self.csv_writers:
                        self.csv_writers[disk].writerow([
                            time.strftime('%Y-%m-%d %H:%M:%S'),
                            rs, ws, rl, wl, rz, wz, iops
                        ])
                        self.log_files[disk].flush()

        self.last_counters = current_counters
        self.last_time = current_time

    def start_logging(self, dir_path, base_name):
        self.log_files = {}
        self.csv_writers = {}
        for disk in self.disks:
            safe_disk = disk.replace('\\', '_').replace('/', '_').replace(':', '')
            # Unpredictable name + exclusive create ('x'): an existing file, symlink
            # or junction at this path is refused instead of followed or truncated.
            filepath = os.path.join(dir_path, f"{base_name}_{secrets.token_hex(4)}_{safe_disk}.csv")
            try:
                f = open(filepath, 'x', newline='')
            except Exception:
                for opened in self.log_files.values():
                    opened.close()
                self.log_files = {}
                self.csv_writers = {}
                raise
            writer = csv.writer(f)
            writer.writerow(['Timestamp', 'Read MB/s', 'Write MB/s', 'Read Latency ms', 'Write Latency ms', 'Read Size KB', 'Write Size KB', 'IOPS'])
            self.log_files[disk] = f
            self.csv_writers[disk] = writer
        self.logging = True

    def stop_logging(self):
        self.logging = False
        for f in self.log_files.values():
            f.close()
        self.log_files = {}
        self.csv_writers = {}

class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("ioTraX - System Storage Monitor")
        self.geometry("1200x800")
        ctk.set_appearance_mode("dark")
        
        self.monitor = IOMonitor()
        
        # Grid layout
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        
        # Sidebar
        self.sidebar = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar_label = ctk.CTkLabel(self.sidebar, text="ioTraX", font=ctk.CTkFont(size=20, weight="bold"))
        self.sidebar_label.pack(pady=20, padx=10)
        
        self.sidebar_drive_label = ctk.CTkLabel(self.sidebar, text="Visible Drives:", font=ctk.CTkFont(size=12, weight="bold"))
        self.sidebar_drive_label.pack(pady=(10, 5), padx=10, anchor="w")

        self.drive_vars = {}
        for disk in self.monitor.disks:
            var = tk.BooleanVar(value=True)
            self.drive_vars[disk] = var
            cb = ctk.CTkCheckBox(self.sidebar, text=disk, variable=var, command=self.update_plots)
            cb.pack(pady=2, padx=15, anchor="w")
            
        # Refresh Interval
        self.interval_label = ctk.CTkLabel(self.sidebar, text="Refresh: 1.0s", font=ctk.CTkFont(size=12))
        self.interval_label.pack(pady=(20, 0), padx=10)
        self.interval_slider = ctk.CTkSlider(self.sidebar, from_=0.1, to=5.0, number_of_steps=49, command=self.change_interval)
        self.interval_slider.set(1.0)
        self.interval_slider.pack(pady=(0, 10), padx=10)

        # Logging Button
        self.log_btn = ctk.CTkButton(self.sidebar, text="Start Logging", command=self.toggle_logging)
        self.log_btn.pack(pady=20, padx=10, side="bottom")

        # Main Scrollable Area
        self.scroll_frame = ctk.CTkScrollableFrame(self, label_text="Live Storage Performance")
        self.scroll_frame.grid(row=0, column=1, sticky="nsew", padx=10, pady=(10, 40))
        
        # Status Bar
        self.status_bar = ctk.CTkFrame(self, height=30, corner_radius=0)
        self.status_bar.grid(row=0, column=1, sticky="sew")
        self.status_label = ctk.CTkLabel(self.status_bar, text="Initializing...", font=ctk.CTkFont(size=11))
        self.status_label.pack(side="left", padx=20)

        self.figures = {} # disk: {fig, canvas}
        self.refresh_ms = 1000
        self.update_plots()
        
        self.schedule_update()

    def change_interval(self, value):
        self.refresh_ms = int(float(value) * 1000)
        self.interval_label.configure(text=f"Refresh: {float(value):.1f}s")

    def toggle_logging(self):
        if not self.monitor.logging:
            default_dir = os.path.expanduser("~/Documents")
            dir_path = filedialog.askdirectory(
                initialdir=default_dir,
                title="Select Folder to Save IO Logs"
            )
            if not dir_path:
                return
            base_name = f"io_log_{int(time.time())}"
            try:
                self.monitor.start_logging(dir_path, base_name)
                self.log_btn.configure(text="Stop Logging", fg_color="red")
            except Exception as e:
                messagebox.showerror("Logging Error", f"Could not start logging:\n{e}")
        else:
            self.monitor.stop_logging()
            self.log_btn.configure(text="Start Logging", fg_color=["#3B8ED0", "#1F6AA5"])

    def update_plots(self):
        # Clear old plots if needed
        for disk, widgets in self.figures.items():
            if not self.drive_vars[disk].get():
                widgets['canvas'].get_tk_widget().pack_forget()
        
        for disk in self.monitor.disks:
            if self.drive_vars[disk].get() and disk not in self.figures:
                fig = Figure(figsize=(8, 6), dpi=90)
                fig.set_facecolor('#2b2b2b')
                
                # Speed Plot
                ax1 = fig.add_subplot(221)
                ax1.set_title(f"{disk} - Throughput (MB/s)", color='white', fontsize=10)
                ax1.set_ylabel("MB/s", color='white', fontsize=8)
                
                # Latency Plot
                ax2 = fig.add_subplot(222)
                ax2.set_title(f"{disk} - Avg Latency (ms)", color='white', fontsize=10)
                ax2.set_ylabel("ms", color='white', fontsize=8)

                # IO Size Plot
                ax3 = fig.add_subplot(223)
                ax3.set_title(f"{disk} - Avg IO Size (KB)", color='white', fontsize=10)
                ax3.set_ylabel("KB", color='white', fontsize=8)

                # IOPS Plot
                ax4 = fig.add_subplot(224)
                ax4.set_title(f"{disk} - IOPS", color='white', fontsize=10)
                ax4.set_ylabel("Ops/s", color='white', fontsize=8)

                for ax in [ax1, ax2, ax3, ax4]:
                    ax.set_facecolor('#1e1e1e')
                    ax.tick_params(colors='gray', labelsize=8)
                    ax.xaxis.set_visible(False)
                    ax.grid(True, color='#333333', linestyle='--')
                    for spine in ax.spines.values():
                        spine.set_color('#444444')

                canvas = FigureCanvasTkAgg(fig, master=self.scroll_frame)
                canvas.get_tk_widget().pack(pady=20, padx=20, fill="x", expand=True)
                
                self.figures[disk] = {
                    'fig': fig,
                    'canvas': canvas,
                    'axes': [ax1, ax2, ax3, ax4],
                    'lines': {
                        'read_speed': ax1.plot([], [], label='Read', color='#00aaff', linewidth=1.5)[0],
                        'write_speed': ax1.plot([], [], label='Write', color='#ff5500', linewidth=1.5)[0],
                        'read_lat': ax2.plot([], [], label='Read', color='#00aaff', linewidth=1.5)[0],
                        'write_lat': ax2.plot([], [], label='Write', color='#ff5500', linewidth=1.5)[0],
                        'read_size': ax3.plot([], [], label='Read', color='#00aaff', linewidth=1.5)[0],
                        'write_size': ax3.plot([], [], label='Write', color='#ff5500', linewidth=1.5)[0],
                        'iops': ax4.plot([], [], label='Total', color='#00ff00', linewidth=1.5)[0],
                    }
                }
                for ax in [ax1, ax2, ax3, ax4]:
                    leg = ax.legend(loc='upper right', facecolor='#2b2b2b', edgecolor='#444444', fontsize=7)
                    for text in leg.get_texts():
                        text.set_color('white')
                fig.tight_layout()
            elif self.drive_vars[disk].get() and disk in self.figures:
                self.figures[disk]['canvas'].get_tk_widget().pack(pady=10, fill="x", expand=True)

    def schedule_update(self):
        try:
            self.monitor.update()
            self.refresh_ui()
        except Exception as e:
            import traceback
            traceback.print_exc()
        self.after(self.refresh_ms, self.schedule_update)

    def refresh_ui(self):
        total_read = 0
        total_write = 0
        for disk, widgets in self.figures.items():
            if self.drive_vars[disk].get():
                data = self.monitor.history[disk]
                x = list(range(MAX_SAMPLES))
                
                rs = list(data['read_speed'])
                ws = list(data['write_speed'])
                total_read += rs[-1]
                total_write += ws[-1]

                widgets['lines']['read_speed'].set_data(x, rs)
                widgets['lines']['write_speed'].set_data(x, ws)
                widgets['lines']['read_lat'].set_data(x, list(data['read_latency']))
                widgets['lines']['write_lat'].set_data(x, list(data['write_latency']))
                widgets['lines']['read_size'].set_data(x, list(data['read_size']))
                widgets['lines']['write_size'].set_data(x, list(data['write_size']))
                widgets['lines']['iops'].set_data(x, list(data['iops']))
                
                for ax in widgets['axes']:
                    ax.relim()
                    ax.autoscale_view()
                
                widgets['canvas'].draw_idle()
        
        self.status_label.configure(text=f"Total System IO - Read: {total_read:.2f} MB/s | Write: {total_write:.2f} MB/s | Logging: {'ON' if self.monitor.logging else 'OFF'}")

if __name__ == "__main__":
    app = App()
    app.mainloop()
