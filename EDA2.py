import sys
import asyncio
import csv
import os
from datetime import datetime
from bleak import BleakScanner, BleakClient
from PyQt5 import QtWidgets, QtCore
import pyqtgraph as pg
from scipy import signal

NORDIC_UART_TX = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"
NORDIC_UART_RX = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"


class AssistDashboard(QtWidgets.QMainWindow):

    def __init__(self):
        super().__init__()

        self.setWindowTitle("ASSIST Physiological Monitoring System")
        self.setGeometry(100, 100, 1700, 950)

        self.save_folder = (
            r"C:\Users\Gound\Desktop\graph values"  # CHANGE if needed
        )

        self.client = None
        self.loop = asyncio.get_event_loop()

        self.recording = False
        self.recorded_rows = []

        self.ecg_data = []
        self.eda_data = []
        # processed (filtered/smoothed) version of the EDA series
        self.eda_filtered = []
        # advanced band‑pass filter parameters (adjust to your sampling rate)
        self.eda_fs = 10.0  # Hz, approximate sample frequency from device
        self.eda_lowcut = 0.01  # Hz
        self.eda_highcut = 1.0  # Hz
        self.eda_order = 2
        nyq = 0.5 * self.eda_fs
        low = self.eda_lowcut / nyq
        high = self.eda_highcut / nyq
        self.eda_b, self.eda_a = signal.butter(
            self.eda_order, [low, high], btype="band"
        )
        # filter state for incremental lfilter calls
        self.eda_z = signal.lfilter_zi(self.eda_b, self.eda_a)

        self.acc_x = []
        self.acc_y = []
        self.acc_z = []

        self.init_ui()

        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.run_async_loop)
        self.timer.start(10)

    # ================= ASYNC LOOP =================
    def run_async_loop(self):
        self.loop.call_soon(self.loop.stop)
        self.loop.run_forever()

    # ================= UI =================
    def init_ui(self):

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        main_layout = QtWidgets.QHBoxLayout()
        central.setLayout(main_layout)

        # LEFT PANEL
        left_layout = QtWidgets.QVBoxLayout()

        self.scan_btn = QtWidgets.QPushButton("Scan Devices")
        self.scan_btn.clicked.connect(self.scan_devices)

        self.connect_btn = QtWidgets.QPushButton("Connect")
        self.connect_btn.clicked.connect(self.connect_device)

        self.start_btn = QtWidgets.QPushButton("START (Send 's')")
        self.start_btn.clicked.connect(self.start_stream)

        self.stop_btn = QtWidgets.QPushButton("STOP (Send 'stop')")
        self.stop_btn.clicked.connect(self.stop_stream)

        self.device_dropdown = QtWidgets.QComboBox()

        self.status_label = QtWidgets.QLabel("Disconnected")
        self.status_label.setStyleSheet("color: red")

        self.console = QtWidgets.QTextEdit()
        self.console.setReadOnly(True)
        self.console.setStyleSheet(
            """
            background-color: black;
            color: #00ff99;
            font-family: Consolas;
        """
        )

        left_layout.addWidget(self.scan_btn)
        left_layout.addWidget(self.connect_btn)
        left_layout.addWidget(self.start_btn)
        left_layout.addWidget(self.stop_btn)
        left_layout.addWidget(self.device_dropdown)
        left_layout.addWidget(self.status_label)
        left_layout.addWidget(QtWidgets.QLabel("Live Data Console"))
        left_layout.addWidget(self.console)

        main_layout.addLayout(left_layout, 1)

        # RIGHT PANEL (GRAPHS)
        self.plot_widget = pg.GraphicsLayoutWidget()
        main_layout.addWidget(self.plot_widget, 3)

        pg.setConfigOptions(antialias=True)

        # ECG
        self.ecg_plot = self.plot_widget.addPlot(title="ECG")
        self.ecg_plot.showGrid(x=True, y=True)
        self.ecg_curve = self.ecg_plot.plot(pen=pg.mkPen("#00f5ff", width=2))
        self.ecg_plot.enableAutoRange(axis="y", enable=True)

        self.plot_widget.nextRow()

        # EDA
        self.eda_plot = self.plot_widget.addPlot(title="EDA (uS)")
        self.eda_plot.showGrid(x=True, y=True)
        # raw and filtered traces overlaid
        self.eda_curve = self.eda_plot.plot(
            pen=pg.mkPen("#38bdf8", width=2), name="raw"
        )
        self.eda_filtered_curve = self.eda_plot.plot(
            pen=pg.mkPen("#ff00ff", width=1), name="filtered"
        )
        self.eda_plot.enableAutoRange(axis="y", enable=True)

        self.plot_widget.nextRow()

        # Accelerometer X
        self.accx_plot = self.plot_widget.addPlot(title="Accelerometer X")
        self.accx_plot.showGrid(x=True, y=True)
        self.accx_curve = self.accx_plot.plot(pen=pg.mkPen("#ef4444", width=2))
        self.accx_plot.enableAutoRange(axis="y", enable=True)

        self.plot_widget.nextRow()

        # Accelerometer Y
        self.accy_plot = self.plot_widget.addPlot(title="Accelerometer Y")
        self.accy_plot.showGrid(x=True, y=True)
        self.accy_curve = self.accy_plot.plot(pen=pg.mkPen("#22c55e", width=2))
        self.accy_plot.enableAutoRange(axis="y", enable=True)

        self.plot_widget.nextRow()

        # Accelerometer Z
        self.accz_plot = self.plot_widget.addPlot(title="Accelerometer Z")
        self.accz_plot.showGrid(x=True, y=True)
        self.accz_curve = self.accz_plot.plot(pen=pg.mkPen("#eab308", width=2))
        self.accz_plot.enableAutoRange(axis="y", enable=True)

    # ================= BLE =================
    def scan_devices(self):
        asyncio.ensure_future(self._scan())

    async def _scan(self):
        self.device_dropdown.clear()
        devices = await BleakScanner.discover(timeout=5)
        for d in devices:
            name = d.name if d.name else "NoName"
            self.device_dropdown.addItem(f"{name} | {d.address}", d.address)

    def connect_device(self):
        address = self.device_dropdown.currentData()
        if address:
            asyncio.ensure_future(self._connect(address))

    async def _connect(self, address):
        self.client = BleakClient(address)
        await self.client.connect()
        await self.client.start_notify(NORDIC_UART_TX, self.notification_handler)
        self.status_label.setText("Connected")
        self.status_label.setStyleSheet("color: #00f5ff")

    async def _send(self, command):
        if self.client and self.client.is_connected:
            await self.client.write_gatt_char(NORDIC_UART_RX, command.encode())

    # ================= START / STOP =================
    def start_stream(self):
        # kick off an async helper which handles sending the start command
        # and (re)starting notifications if they were stopped earlier
        asyncio.ensure_future(self._start_stream())

    async def _start_stream(self):
        if not self.client or not self.client.is_connected:
            self.console.append(">>> Device not connected")
            return

        try:
            await self._send("s")
        except Exception as e:
            self.console.append(f">>> Error sending start command: {e}")
            return

        # If notifications were stopped by a previous stop_stream call we
        # need to restart them so the handler will be invoked again.
        try:
            if self.client and self.client.is_connected:
                await self.client.start_notify(
                    NORDIC_UART_TX, self.notification_handler
                )
        except Exception as e:
            # log but continue; device may already be notifying
            self.console.append(f">>> Error starting notifications: {e}")

        # reset recording buffers and plot data
        self.recorded_rows = []
        self.recording = True
        self.ecg_data.clear()
        self.eda_data.clear()
        self.eda_filtered.clear()
        # reset filter state so new stream starts fresh
        self.eda_z = signal.lfilter_zi(self.eda_b, self.eda_a)
        self.acc_x.clear()
        self.acc_y.clear()
        self.acc_z.clear()
        # clear curves as well (in case notify restarts immediately)
        self.ecg_curve.setData([])
        self.eda_curve.setData([])
        self.eda_filtered_curve.setData([])
        self.accx_curve.setData([])
        self.accy_curve.setData([])
        self.accz_curve.setData([])

        self.console.append(">>> START command sent. Recording started.")

    def stop_stream(self):
        if not self.client or not self.client.is_connected:
            return
        asyncio.ensure_future(self._stop_and_save())

    async def _stop_and_save(self):
        """Send stop command, stop notifications, and write recorded data to a new file."""
        try:
            await self._send("stop")
        except Exception as e:
            self.console.append(f">>> Error sending stop command: {e}")

        # attempt to stop notifications so device stops publishing
        try:
            if self.client and self.client.is_connected:
                await self.client.stop_notify(NORDIC_UART_TX)
        except Exception:
            pass

        # stop recording (prevents further rows being appended)
        self.recording = False

        if not self.recorded_rows:
            self.console.append(">>> No data recorded.")
            return

        # ensure save folder exists
        try:
            os.makedirs(self.save_folder, exist_ok=True)
        except Exception as e:
            self.console.append(f">>> Error creating save folder: {e}")

        filename = f"ASSIST_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        full_path = os.path.join(self.save_folder, filename)

        try:
            with open(full_path, mode="w", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(
                    ["Time", "ECG", "EDA", "EDA_filtered", "Acc_X", "Acc_Y", "Acc_Z"]
                )
                writer.writerows(self.recorded_rows)

            self.console.append(
                f">>> STOP command sent. Notifications stopped. File saved to {full_path}"
            )
        except Exception as e:
            self.console.append(f">>> Error saving file: {e}")

    # ================= DATA =================
    def notification_handler(self, sender, data):

        decoded = data.decode(errors="ignore").strip()
        self.console.append(decoded)

        parts = decoded.split()

        try:
            if len(parts) == 3:
                ax, ay, az = map(float, parts)
                self.acc_x.append(ax)
                self.acc_y.append(ay)
                self.acc_z.append(az)

            elif "uS" in decoded:
                eda = float(decoded.replace("uS", "").strip())
                self.eda_data.append(eda)

                # apply advanced IIR band‑pass filter incrementally
                try:
                    # lfilter returns (filtered_values, new_state)
                    filtered_val, self.eda_z = signal.lfilter(
                        self.eda_b, self.eda_a, [eda], zi=self.eda_z
                    )
                    self.eda_filtered.append(filtered_val[0])
                except Exception:
                    # fallback to raw if filtering fails
                    self.eda_filtered.append(eda)

            elif len(parts) == 1:
                ecg = float(parts[0])
                self.ecg_data.append(ecg)

            # update plots; include filtered trace if available
            self.ecg_curve.setData(self.ecg_data[-500:])
            self.eda_curve.setData(self.eda_data[-500:])
            if self.eda_filtered:
                self.eda_filtered_curve.setData(self.eda_filtered[-500:])
            self.accx_curve.setData(self.acc_x[-500:])
            self.accy_curve.setData(self.acc_y[-500:])
            self.accz_curve.setData(self.acc_z[-500:])

            if self.recording:
                timestamp = datetime.now().strftime("%H:%M:%S.%f")
                # if filtered exists, record it too (empty string if not)
                filt_val = self.eda_filtered[-1] if self.eda_filtered else ""
                self.recorded_rows.append(
                    [
                        timestamp,
                        self.ecg_data[-1] if self.ecg_data else "",
                        self.eda_data[-1] if self.eda_data else "",
                        filt_val,
                        self.acc_x[-1] if self.acc_x else "",
                        self.acc_y[-1] if self.acc_y else "",
                        self.acc_z[-1] if self.acc_z else "",
                    ]
                )

        except:
            pass


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    window = AssistDashboard()
    window.show()
    sys.exit(app.exec_())
