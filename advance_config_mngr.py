import sys
import json
import time
from pathlib import Path
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, 
                             QVBoxLayout, QLineEdit, QPushButton, 
                             QLabel, QMessageBox)
from PyQt6.QtCore import QTimer

# Import your existing backend RAM bus from franken5.py
from franken5 import SharedMemoryBus

class SystemConductor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("System Conductor - Life OS")
        self.setGeometry(100, 100, 450, 350)
        
        # 1. Initialize the shared memory tracks (The Foundation)
        self.system_bus = SharedMemoryBus()
        
        # 2. Establish the SINGLE source of truth file on her Desktop
        self.desktop_config = Path.home() / "Desktop" / "active_system_config.json"
        
        # Track the file's modification timestamp for manual edits (Hot-swapping)
        self.last_modified_time = 0
        
        # 3. Assemble the User Dashboard
        self.init_ui()
        
        # 4. Deploy or read the baseline config file
        self.load_or_deploy_config()
        
        # 5. Start a background heartbeat timer to watch for manual changes
        self.watch_timer = QTimer()
        self.watch_timer.timeout.connect(self.poll_desktop_file_changes)
        self.watch_timer.start(1000) # Check the single file once every second

    def init_ui(self):
        """Builds a clean, non-cluttered layout for her settings"""
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        
        layout.addWidget(QLabel("<b>Sector 1 (Physics) Processing Speed:</b>"))
        self.speed_input = QLineEdit("1.75")
        layout.addWidget(self.speed_input)
        
        layout.addWidget(QLabel("<b>Active Sector Assignment (1-8):</b>"))
        self.sector_input = QLineEdit("4")
        layout.addWidget(self.sector_input)
        
        # The single action button to push configurations
        self.apply_btn = QPushButton("⚡ Save Config & Update Live System")
        self.apply_btn.clicked.connect(self.save_from_gui)
        layout.addWidget(self.apply_btn)
        
        self.status_label = QLabel("System Status: Linked to RAM Bus")
        layout.addWidget(self.status_label)

    def load_or_deploy_config(self):
        """Ensures the single desktop file exists and syncs it to RAM on startup"""
        if not self.desktop_config.exists():
            default_payload = {"physics_speed": 1.75, "active_sector": 4}
            with open(self.desktop_config, "w", encoding="utf-8") as f:
                json.dump(default_payload, f, indent=4)
        
        self.last_modified_time = self.desktop_config.stat().st_mtime
        self.stream_file_to_system_ram()

    def stream_file_to_system_ram(self):
        """Reads the single desktop file directly and blasts it straight into RAM Slot 0"""
        try:
            # Read raw bytes straight from the single file path
            raw_bytes = self.desktop_config.read_bytes()
            
            # Write directly to Channel 1 (Slot 0) on the hardware memory bus
            self.system_bus.write_stage(slot=0, data=raw_bytes)
            
            # Parse it locally just to sync our GUI display fields automatically
            data = json.loads(raw_bytes.decode('utf-8'))
            self.speed_input.setText(str(data.get("physics_speed", 1.75)))
            self.sector_input.setText(str(data.get("active_sector", 4)))
            
            self.status_label.setText(f"System Status: Live Synced ({time.strftime('%H:%M:%S')})")
        except Exception as e:
            self.status_label.setText(f"Sync Error: {e}")

    def save_from_gui(self):
        """Triggered when she hits the button: Writes to Desktop, then updates RAM"""
        try:
            payload = {
                "physics_speed": float(self.speed_input.text()),
                "active_sector": int(self.sector_input.text())
            }
            
            # Write out to the SINGLE desktop file
            with open(self.desktop_config, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=4)
            
            # Instantly update the modification timestamp so the watcher doesn't double-trigger
            self.last_modified_time = self.desktop_config.stat().st_mtime
            
            # Stream the updated file directly to the memory channels
            self.stream_file_to_system_ram()
            QMessageBox.information(self, "Success", "Live system provisioned and Desktop file updated!")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Load line failed: {e}")

    def poll_desktop_file_changes(self):
        """Watches the single desktop file. If she edits it with Notepad, it hot-swaps live!"""
        try:
            current_mtime = self.desktop_config.stat().st_mtime
            if current_mtime > self.last_modified_time:
                self.last_modified_time = current_mtime
                print("⚡ Manual desktop file edit detected! Hot-swapping system RAM...")
                self.stream_file_to_system_ram()
        except:
            pass # Protects the app from crashing if she saves a half-typed file

def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    conductor = SystemConductor()
    conductor.show()
    sys.exit(app.exec())

if __name__ == '__main__':
    main()

