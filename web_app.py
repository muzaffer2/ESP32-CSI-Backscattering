from flask import Flask, render_template, jsonify, request
import serial
import json
import re
import datetime
import csv
import threading
import time
import os
from collections import deque
import math
import uuid

app = Flask(__name__)

class CSIDataLogger:
    def __init__(self, port, baud_rate=115200):
        # Basic serial connection settings
        self.port = port
        self.baud_rate = baud_rate
        self.serial_conn = None
        self.csv_writer = None
        self.csv_file = None
        self.is_running = False
        self.packet_count = 0
        self.session_start_time = None
        
        # Each logging session gets its own directory with a unique ID
        # This helps keep data organized when doing multiple experiments
        self.session_id = str(uuid.uuid4())[:8]
        self.session_dir = f"sessions/session-{self.session_id}"
        os.makedirs(self.session_dir, exist_ok=True)
        
        # Keep track of recent packets for the web display
        # Using a deque with maxlen=100 means we only keep the last 100 packets
        # This prevents memory from growing too large during long sessions
        self.recent_data = deque(maxlen=100)
        self.latest_packet = {}
        
        # Store data points for plotting
        # We keep 200 points for smooth scrolling plots
        self.plot_data = deque(maxlen=200)
        
        # Track which subcarriers we've seen data for
        # This helps populate the dropdown menu in the web UI
        self.available_subcarriers = set()
        
        # Save raw serial lines for debugging
        self.raw_lines = deque(maxlen=50)
        
    def connect(self):
        """Try to connect to the ESP32 over serial port"""
        try:
            self.serial_conn = serial.Serial(self.port, self.baud_rate, timeout=1)
            print(f"Connected to ESP32 on {self.port}")
            return True
        except serial.SerialException as e:
            print(f"Failed to connect: {e}")
            return False

    def setup_csv_file(self):
        """Create a new CSV file for this logging session"""
        try:
            # Create filename with timestamp so we know when the data was collected
            timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"csi_data_{timestamp}.csv"
            filepath = os.path.join(self.session_dir, filename)
            
            # Ensure directory exists
            os.makedirs(self.session_dir, exist_ok=True)
            print(f"[CSV] Session directory: {os.path.abspath(self.session_dir)}")
            
            self.csv_file = open(filepath, 'w', newline='')
            print(f"[CSV] File opened: {os.path.abspath(filepath)}")
            
            # Define what data we'll store in each row
            fieldnames = [
                'timestamp', 'rssi', 'rate', 'channel', 'bandwidth', 
                'data_length', 'esp_timestamp', 'csi_data'
            ]
            
            self.csv_writer = csv.DictWriter(self.csv_file, fieldnames=fieldnames)
            self.csv_writer.writeheader()
            self.csv_file.flush()
            
            print(f"[CSV] CSV writer initialized. File: {filepath}")
            print(f"[CSV] Headers written: {fieldnames}")
            return filepath
        except Exception as e:
            print(f"[ERROR] Failed to setup CSV file: {e}")
            import traceback
            traceback.print_exc()
            raise
    
    def parse_csi_line(self, line):
        """Extract CSI data from the ESP32's output format
        
        The ESP32 sends data in this format:
        CSI_START{"rssi":-85,"rate":11,"channel":11,"bandwidth":0,"len":128,"timestamp":50136694,"csi_data":[...]}CSI_END
        
        We need to:
        1. Find the JSON data between CSI_START and CSI_END
        2. Parse it into a Python dictionary
        3. Return the parsed data or None if something goes wrong
        """
        # Try different regex patterns to handle various formats
        patterns = [
            r'CSI_START(\{[^{}]*\})CSI_END',  # Original strict pattern
            r'CSI_START(\{.*?\})CSI_END',     # Non-greedy pattern
            r'\{["rssi".*?csi_data.*?\]\}'  # Direct JSON pattern
        ]
        
        match = None
        for pattern in patterns:
            match = re.search(pattern, line)
            if match:
                break
        
        if match:
            try:
                json_str = match.group(1) if '"' in match.group(1) else match.group(0)
                data = json.loads(json_str)
                print(f"[SUCCESS] Parsed CSI packet: RSSI={data.get('rssi')}dBm, CH={data.get('channel')}, LEN={len(data.get('csi_data', []))} subcarriers")
                return data
            except json.JSONDecodeError as e:
                print(f"[ERROR] JSON parse failed: {e}")
                print(f"[ERROR] Attempted to parse: {json_str[:200]}...")
        else:
            print(f"[WARNING] No CSI_START/CSI_END found in: {line[:100]}...")
        
        return None
    
    def analyze_csi_structure(self, csi_data):
        """Figure out what CSI data we're getting from the ESP32
        
        The CSI data is an array of values, where each value represents
        a subcarrier. We keep track of which subcarriers we've seen
        so we can show them in the web UI's dropdown menu.
        """
        if not csi_data or not isinstance(csi_data, list):
            return {}
        
        # Add each subcarrier index to our set of available ones
        for i in range(len(csi_data)):
            self.available_subcarriers.add(i)
        
        return {'total_subcarriers': len(csi_data)}
    
    def extract_subcarrier_data(self, csi_data, subcarrier_indices):
        """Get the raw values for specific subcarriers
        
        The ESP32 sends CSI data as an array of integers, where each
        integer represents the signal strength for that subcarrier.
        This function extracts just the values we want to plot.
        """
        if not csi_data:
            print("No CSI data provided")
            return {}
        
        result = {}
        
        for idx in subcarrier_indices:
            try:
                if idx < len(csi_data):
                    # Get the raw value for this subcarrier
                    value = csi_data[idx]
                    result[f'subcarrier_{idx}'] = value
                else:
                    print(f"Subcarrier {idx} index out of range (len={len(csi_data)})")
                    result[f'subcarrier_{idx}'] = 0
                    
            except (TypeError, ValueError, IndexError) as e:
                print(f"Error processing subcarrier {idx}: {e}")
                result[f'subcarrier_{idx}'] = 0
        
        return result
    
    def start_logging(self):
        """Start collecting CSI data in a background thread
        
        This function:
        1. Checks if we're already connected and not already logging
        2. Creates a new CSV file for this session
        3. Starts a background thread to read data from the ESP32
        """
        if not self.serial_conn:
            print("Not connected to ESP32")
            return False
        
        if self.is_running:
            print("Already logging")
            return False
            
        self.is_running = True
        self.session_start_time = time.time()
        self.csv_filename = self.setup_csv_file()
        
        # Start the logging loop in a separate thread
        # This keeps the web UI responsive while we collect data
        self.logging_thread = threading.Thread(target=self._log_loop)
        self.logging_thread.daemon = True  # Thread will exit when main program exits
        self.logging_thread.start()
        
        return True
    
    def _log_loop(self):
        """Main loop that reads data from the ESP32
        
        This function runs in a background thread and:
        1. Reads data from the serial port
        2. Parses the CSI data
        3. Saves it to CSV
        4. Updates the data structures used by the web UI
        """
        try:
            print("Starting CSI data collection...")
            print(f"CSV file: {self.csv_filename}")
            print(f"CSV writer initialized: {self.csv_writer is not None}")
            
            while self.is_running:
                if self.serial_conn and self.serial_conn.in_waiting > 0:
                    try:
                        # Read a line from the ESP32
                        line = self.serial_conn.readline().decode('utf-8', errors='ignore').strip()
                        # store raw line for debugging
                        if line:
                            self.raw_lines.append(line)
                        
                        if line:
                            # Try to parse the CSI data
                            csi_data = self.parse_csi_line(line)
                            
                            if csi_data:
                                try:
                                    python_timestamp = datetime.datetime.now().isoformat()
                                    current_time = time.time()
                                    
                                    # Get the CSI array and analyze its structure
                                    csi_array = csi_data.get('csi_data', [])
                                    self.analyze_csi_structure(csi_array)
                                    
                                    # Prepare the row for the CSV file
                                    row = {
                                        'timestamp': python_timestamp,
                                        'rssi': csi_data.get('rssi', ''),
                                        'rate': csi_data.get('rate', ''),
                                        'channel': csi_data.get('channel', ''),
                                        'bandwidth': csi_data.get('bandwidth', ''),
                                        'data_length': csi_data.get('data_length', ''),  # Changed from 'len' to 'data_length'
                                        'esp_timestamp': csi_data.get('esp_timestamp', ''),  # Changed from 'timestamp' to 'esp_timestamp'
                                        'csi_data': json.dumps(csi_array)
                                    }
                                    
                                    # Save to CSV - with extra error checking
                                    if self.csv_writer and self.csv_file:
                                        try:
                                            self.csv_writer.writerow(row)
                                            self.csv_file.flush()  # Make sure data is written to disk
                                            print(f"[CSV] Wrote packet #{self.packet_count + 1} to {self.csv_filename}")
                                        except Exception as csv_error:
                                            print(f"[ERROR] Failed to write CSV row: {csv_error}")
                                    else:
                                        print(f"[ERROR] CSV writer or file not initialized! Writer={self.csv_writer}, File={self.csv_file}")
                                    
                                    # Update the data structures used by the web UI
                                    self.packet_count += 1
                                    display_data = {
                                        'packet_num': self.packet_count,
                                        'timestamp': python_timestamp,
                                        'rssi': csi_data.get('rssi', 0),
                                        'rate': csi_data.get('rate', 0),
                                        'channel': csi_data.get('channel', 0),
                                        'bandwidth': csi_data.get('bandwidth', 0),
                                        'data_length': csi_data.get('len', 0),
                                        'esp_timestamp': csi_data.get('esp_timestamp', 0),
                                        'time_passed': current_time - self.session_start_time if self.session_start_time else 0
                                    }
                                    
                                    # Add subcarrier data to display
                                    for i in range(len(csi_array)):
                                        display_data[f'subcarrier_{i}'] = csi_array[i]
                                    
                                    # Update the data structures for the web UI
                                    self.recent_data.append(display_data)
                                    self.latest_packet = display_data
                                    
                                    # Store data for plotting
                                    plot_point = {
                                        'time': current_time,
                                        'rssi': csi_data.get('rssi', 0)
                                    }
                                    
                                    # Add all CSI values to the plot data
                                    for i in range(len(csi_array)):
                                        plot_point[f'subcarrier_{i}'] = csi_array[i]
                                    
                                    self.plot_data.append(plot_point)
                                    
                                except Exception as e:
                                    print(f"[ERROR] Error processing parsed CSI data: {e}")
                                    import traceback
                                    traceback.print_exc()
                            
                            else:
                                # Print non-CSI output from the ESP32 (connection messages, etc.)
                                print(f"ESP32: {line}")
                    except Exception as e:
                        print(f"Error processing serial line: {e}")
                        import traceback
                        traceback.print_exc()
                
                # Small delay to prevent using too much CPU
                time.sleep(0.01)
                
        except Exception as e:
            print(f"Logging error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.is_running = False
    
    def stop_logging(self):
        """Stop collecting CSI data and clean up"""
        self.is_running = False
        if hasattr(self, 'logging_thread'):
            self.logging_thread.join(timeout=1)  # Wait up to 1 second for thread to finish
    
    def get_status(self):
        """Get the current status of the logger
        
        Returns a dictionary with:
        - Whether we're connected to the ESP32
        - Whether we're currently logging
        - How many packets we've collected
        - The serial port we're using
        - The current session ID and directory
        """
        return {
            'connected': self.serial_conn and self.serial_conn.is_open,
            'logging': self.is_running,
            'packet_count': self.packet_count,
            'port': self.port,
            'session_id': self.session_id,
            'session_dir': self.session_dir
        }
    
    def get_recent_data(self):
        """Get the last 100 packets for the web UI's data log"""
        return list(self.recent_data)
    
    def get_latest_packet(self):
        """Get the most recent packet for the web UI's latest data display"""
        return self.latest_packet
    
    def get_available_subcarriers(self):
        """Get a list of subcarriers we've seen data for
        
        This is used to populate the dropdown menu in the web UI
        where users can select which subcarriers to plot.
        """
        return sorted(list(self.available_subcarriers))
    
    def get_raw_lines(self):
        """Return recent raw lines received from the serial port for debugging"""
        return list(self.raw_lines)
    
    def get_plot_data(self, selected_subcarriers=None):
        """Return data formatted for plotting with configurable subcarriers"""
        if not self.plot_data:
            return {'time': [], 'rssi': [], 'subcarriers': {}}
        
        if selected_subcarriers is None:
            selected_subcarriers = [1, 5, 9, 13]  # Default
        
        print(f"Getting plot data for subcarriers: {selected_subcarriers}")  # Debug log
        
        # Get current time to calculate relative timestamps
        current_time = time.time()
        
        # Convert to relative time (seconds ago) for easier plotting
        plot_formatted = {
            'time': [],
            'rssi': [],
            'subcarriers': {}
        }
        
        # Initialize subcarrier data
        for sc in selected_subcarriers:
            key = f'subcarrier_{sc}'
            plot_formatted['subcarriers'][key] = []
        
        # Only use the last 100 points
        recent_points = list(self.plot_data)[-100:]
        print(f"Number of recent points: {len(recent_points)}")  # Debug log
        
        for point in recent_points:
            relative_time = point['time'] - current_time  # This will be negative (seconds ago)
            plot_formatted['time'].append(relative_time)
            plot_formatted['rssi'].append(point.get('rssi', 0))
            
            # Add subcarrier data
            for sc in selected_subcarriers:
                key = f'subcarrier_{sc}'
                value = point.get(key, 0)
                plot_formatted['subcarriers'][key].append(value)
                if len(plot_formatted['subcarriers'][key]) == 1:  # Debug log first value
                    print(f"First value for {key}: {value}")
        
        return plot_formatted
    
    def close(self):
        self.stop_logging()
        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.close()
        if self.csv_file:
            self.csv_file.close()

# Global logger instance
logger = None

@app.route('/')
def home():
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>ESP32 CSI Data Monitor with Configurable Plots</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/3.9.1/chart.min.js"></script>
        <style>
            :root {
                --primary-color: #36311F;  /* Dark brown */
                --secondary-color: #59544B;  /* Medium brown */
                --accent-color: #79A9D1;  /* Light blue */
                --success-color: #7D8CA3;  /* Blue-gray */
                --warning-color: #59544B;  /* Medium brown */
                --danger-color: #36311F;  /* Dark brown */
                --light-bg: #F5F6F8;  /* Very light gray */
                --dark-text: #36311F;  /* Dark brown */
                --light-text: #ffffff;
            }

            body { 
                font-family: 'Space Grotesk', 'IBM Plex Mono', monospace;
                margin: 0;
                padding: 20px;
                background: var(--light-bg);
                color: var(--dark-text);
                line-height: 1.6;
            }

            .container { 
                max-width: 1400px; 
                margin: 0 auto;
                padding: 20px;
            }

            h1 {
                color: var(--primary-color);
                font-size: 2.2em;
                margin-bottom: 1.5em;
                text-align: center;
                font-weight: 700;
                text-transform: uppercase;
                letter-spacing: 2px;
                font-family: 'Space Grotesk', sans-serif;
            }

            h3 {
                color: var(--primary-color);
                font-size: 1.4em;
                margin-bottom: 1em;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 1px;
                font-family: 'Space Grotesk', sans-serif;
            }

            .card { 
                background: white; 
                padding: 25px; 
                margin: 20px 0; 
                border-radius: 0; 
                box-shadow: 0 4px 6px rgba(0,0,0,0.1);
                border-left: 4px solid var(--accent-color);
            }

            .status { 
                display: flex; 
                gap: 20px; 
                align-items: center; 
                flex-wrap: wrap;
                margin-bottom: 20px;
            }

            .status-item { 
                padding: 12px 20px; 
                border-radius: 0; 
                font-weight: 600;
                font-size: 0.95em;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                text-transform: uppercase;
                letter-spacing: 1px;
                font-family: 'Space Grotesk', sans-serif;
            }

            .connected { 
                background: var(--accent-color); 
                color: var(--light-text);
            }

            .disconnected { 
                background: var(--danger-color); 
                color: var(--light-text);
            }

            .logging { 
                background: var(--success-color); 
                color: var(--light-text);
            }

            .stopped { 
                background: var(--warning-color); 
                color: var(--light-text);
            }

            button { 
                padding: 12px 24px; 
                margin: 5px; 
                border: none; 
                border-radius: 0; 
                cursor: pointer; 
                font-size: 0.95em;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 1px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.2);
                font-family: 'Space Grotesk', sans-serif;
            }

            .btn-primary { 
                background: var(--accent-color); 
                color: var(--light-text);
            }

            .btn-success { 
                background: var(--success-color); 
                color: var(--light-text);
            }

            .btn-danger { 
                background: var(--danger-color); 
                color: var(--light-text);
            }

            .btn-warning { 
                background: var(--warning-color); 
                color: var(--light-text);
            }

            .data-display { 
                font-family: 'IBM Plex Mono', monospace; 
                font-size: 0.9em;
            }

            .latest-data { 
                display: grid; 
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); 
                gap: 15px;
            }

            .data-item { 
                background: var(--light-bg); 
                padding: 15px; 
                border-radius: 0;
                font-size: 0.95em;
                border-left: 3px solid var(--accent-color);
                font-family: 'IBM Plex Mono', monospace;
            }

            #data-log { 
                height: 300px; 
                overflow-y: scroll; 
                border: 1px solid #ddd; 
                padding: 15px; 
                background: var(--light-bg);
                border-radius: 0;
                font-family: 'IBM Plex Mono', monospace;
                font-size: 0.9em;
            }

            .chart-container { 
                height: 400px; 
                margin: 20px 0;
                background: white;
                padding: 20px;
                border-radius: 0;
                box-shadow: 0 4px 6px rgba(0,0,0,0.1);
                border-left: 4px solid var(--accent-color);
            }

            .plots-container { 
                display: grid; 
                grid-template-columns: 1fr 1fr; 
                gap: 30px;
            }

            .plot-controls { 
                display: flex; 
                gap: 20px; 
                align-items: center; 
                margin-bottom: 20px; 
                flex-wrap: wrap;
                background: var(--light-bg);
                padding: 15px;
                border-radius: 0;
                border-left: 4px solid var(--accent-color);
            }

            .control-group { 
                display: flex; 
                align-items: center; 
                gap: 10px;
            }

            select, input[type="text"] { 
                padding: 10px 15px; 
                border: 2px solid var(--accent-color); 
                border-radius: 0;
                font-size: 0.95em;
                font-family: 'IBM Plex Mono', monospace;
            }

            select:focus, input[type="text"]:focus {
                border-color: var(--primary-color);
                outline: none;
            }

            .multi-select { 
                min-width: 200px;
            }

            @media (max-width: 1200px) {
                .plots-container { 
                    grid-template-columns: 1fr; 
                }
                
                .container {
                    padding: 10px;
                }
                
                .card {
                    padding: 15px;
                }
            }

            /* Custom scrollbar */
            ::-webkit-scrollbar {
                width: 8px;
            }

            ::-webkit-scrollbar-track {
                background: var(--light-bg);
            }

            ::-webkit-scrollbar-thumb {
                background: var(--accent-color);
            }

            ::-webkit-scrollbar-thumb:hover {
                background: var(--primary-color);
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>ESP32 Real-Time CSI Data Monitor</h1>
            
            <div class="card">
                <h3>Connection Status</h3>
                <div class="status" id="status">
                    <div class="status-item disconnected">Disconnected</div>
                    <div class="status-item stopped">Not Logging</div>
                    <div>Packets: <span id="packet-count">0</span></div>
                    <div>Session: <span id="session-id">None</span></div>
                </div>
                
                <div style="margin-top: 15px;">
                    <input type="text" id="port-input" placeholder="COM5 or /dev/ttyUSB0" style="padding: 8px; width: 200px;">
                    <button class="btn-primary" onclick="connect()">Connect</button>
                    <button class="btn-danger" onclick="disconnect()">Disconnect</button>
                    <button class="btn-success" onclick="startLogging()">Start Logging</button>
                    <button class="btn-warning" onclick="stopLogging()">Stop Logging</button>
                </div>
            </div>
            
            <div class="card">
                <h3>Latest CSI Data</h3>
                <div class="latest-data" id="latest-data">
                    <div class="data-item">No data yet...</div>
                </div>
            </div>
            
            <div class="card">
                <h3>Real-time Plots</h3>
                <div class="plot-controls">
                    <div class="control-group">
                        <label>Subcarriers:</label>
                        <select id="subcarrier1" class="subcarrier-select">
                            <option value="0">Subcarrier 0</option>
                            <option value="1" selected>Subcarrier 1</option>
                            <option value="2">Subcarrier 2</option>
                            <!-- Add options 3-127 -->
                        </select>
                        <select id="subcarrier2" class="subcarrier-select">
                            <option value="0">Subcarrier 0</option>
                            <option value="1">Subcarrier 1</option>
                            <option value="2">Subcarrier 2</option>
                            <!-- Add options 3-127 -->
                        </select>
                        <select id="subcarrier3" class="subcarrier-select">
                            <option value="0">Subcarrier 0</option>
                            <option value="1">Subcarrier 1</option>
                            <option value="2">Subcarrier 2</option>
                            <!-- Add options 3-127 -->
                        </select>
                        <select id="subcarrier4" class="subcarrier-select">
                            <option value="0">Subcarrier 0</option>
                            <option value="1">Subcarrier 1</option>
                            <option value="2">Subcarrier 2</option>
                            <!-- Add options 3-127 -->
                        </select>
                    </div>
                    <button class="btn-primary" onclick="updatePlotConfig()">Update Plots</button>
                </div>
                <div class="plots-container">
                    <div class="chart-container">
                        <canvas id="rssiChart"></canvas>
                    </div>
                    <div class="chart-container">
                        <canvas id="subcarrierChart"></canvas>
                    </div>
                </div>
            </div>
            
            <div class="card">
                <h3>Data Log</h3>
                <div id="data-log"></div>
            </div>
        </div>
        
        <script>
            let selectedSubcarriers = [1, 5, 9, 13];
            
            // Chart configurations
            const rssiChart = new Chart(document.getElementById('rssiChart'), {
                type: 'line',
                data: {
                    labels: [],
                    datasets: [{
                        label: 'RSSI (dBm)',
                        data: [],
                        borderColor: 'rgb(255, 99, 132)',
                        backgroundColor: 'rgba(255, 99, 132, 0.2)',
                        tension: 0.1
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        title: {
                            display: true,
                            text: 'RSSI over Time'
                        }
                    },
                    scales: {
                        x: {
                            title: {
                                display: true,
                                text: 'Time (seconds ago)'
                            }
                        },
                        y: {
                            title: {
                                display: true,
                                text: 'RSSI (dBm)'
                            }
                        }
                    },
                    animation: {
                        duration: 0
                    }
                }
            });
            
            const subcarrierChart = new Chart(document.getElementById('subcarrierChart'), {
                type: 'line',
                data: {
                    labels: [],
                    datasets: []
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        title: {
                            display: true,
                            text: 'Subcarrier Values over Time'
                        }
                    },
                    scales: {
                        x: {
                            title: {
                                display: true,
                                text: 'Time (seconds ago)'
                            }
                        },
                        y: {
                            title: {
                                display: true,
                                text: 'Value'
                            }
                        }
                    },
                    animation: {
                        duration: 0
                    }
                }
            });
            
            // Initialize subcarrier dropdowns with all 128 options
            function initializeSubcarrierDropdowns() {
                const dropdowns = ['subcarrier1', 'subcarrier2', 'subcarrier3', 'subcarrier4'];
                dropdowns.forEach((id, index) => {
                    const select = document.getElementById(id);
                    select.innerHTML = '';
                    for (let i = 0; i < 128; i++) {
                        const option = document.createElement('option');
                        option.value = i;
                        option.textContent = `Subcarrier ${i}`;
                        if (i === selectedSubcarriers[index]) {
                            option.selected = true;
                        }
                        select.appendChild(option);
                    }
                });
            }
            
            function updatePlotConfig() {
                selectedSubcarriers = [
                    parseInt(document.getElementById('subcarrier1').value),
                    parseInt(document.getElementById('subcarrier2').value),
                    parseInt(document.getElementById('subcarrier3').value),
                    parseInt(document.getElementById('subcarrier4').value)
                ];
                
                // Update chart title
                subcarrierChart.options.plugins.title.text = 'Subcarrier Values over Time';
                subcarrierChart.options.scales.y.title.text = 'Value';
                
                // Clear existing datasets
                subcarrierChart.data.datasets = [];
                
                // Create new datasets
                const colors = [
                    'rgb(54, 162, 235)',
                    'rgb(255, 205, 86)', 
                    'rgb(75, 192, 192)',
                    'rgb(153, 102, 255)'
                ];
                
                selectedSubcarriers.forEach((sc, index) => {
                    const color = colors[index % colors.length];
                    subcarrierChart.data.datasets.push({
                        label: `Subcarrier ${sc}`,
                        data: [],
                        borderColor: color,
                        backgroundColor: color.replace('rgb', 'rgba').replace(')', ', 0.2)'),
                        tension: 0.1
                    });
                });
                
                subcarrierChart.update();
            }
            
            function updateCharts() {
                const params = new URLSearchParams({
                    subcarriers: selectedSubcarriers.join(',')
                });
                
                fetch('/api/plot_data?' + params)
                    .then(response => response.json())
                    .then(data => {
                        if (data.time && data.time.length > 0) {
                            // Update RSSI chart
                            rssiChart.data.labels = data.time;
                            rssiChart.data.datasets[0].data = data.rssi;
                            rssiChart.update('none');
                            
                            // Update subcarrier chart
                            subcarrierChart.data.labels = data.time;
                            selectedSubcarriers.forEach((sc, index) => {
                                const key = `subcarrier_${sc}`;
                                if (subcarrierChart.data.datasets[index] && data.subcarriers[key]) {
                                    subcarrierChart.data.datasets[index].data = data.subcarriers[key];
                                }
                            });
                            subcarrierChart.update('none');
                        }
                    })
                    .catch(error => console.error('Error updating charts:', error));
            }
            
            function updateStatus() {
                fetch('/api/status')
                    .then(response => response.json())
                    .then(data => {
                        const statusDiv = document.getElementById('status');
                        const connClass = data.connected ? 'connected' : 'disconnected';
                        const connText = data.connected ? 'Connected' : 'Disconnected';
                        const logClass = data.logging ? 'logging' : 'stopped';
                        const logText = data.logging ? 'Logging' : 'Not Logging';
                        
                        statusDiv.innerHTML = `
                            <div class="status-item ${connClass}">${connText} ${data.port ? '(' + data.port + ')' : ''}</div>
                            <div class="status-item ${logClass}">${logText}</div>
                            <div>Packets: <span id="packet-count">${data.packet_count}</span></div>
                            <div>Session: <span id="session-id">${data.session_id || 'None'}</span></div>
                        `;
                    });
            }
            
            function formatTimePassed(seconds) {
                if (seconds < 60) {
                    return `${seconds.toFixed(1)}s`;
                } else if (seconds < 3600) {
                    const minutes = Math.floor(seconds / 60);
                    const secs = Math.floor(seconds % 60);
                    return `${minutes}m ${secs}s`;
                } else {
                    const hours = Math.floor(seconds / 3600);
                    const minutes = Math.floor((seconds % 3600) / 60);
                    return `${hours}h ${minutes}m`;
                }
            }
            
            function updateLatestData() {
                fetch('/api/latest')
                    .then(response => response.json())
                    .then(data => {
                        if (Object.keys(data).length > 0) {
                            const latestDiv = document.getElementById('latest-data');
                            latestDiv.innerHTML = `
                                <div class="data-item"><strong>Packet #:</strong> ${data.packet_num}</div>
                                <div class="data-item"><strong>RSSI:</strong> ${data.rssi} dBm</div>
                                <div class="data-item"><strong>Rate:</strong> ${data.rate}</div>
                                <div class="data-item"><strong>Channel:</strong> ${data.channel}</div>
                                <div class="data-item"><strong>Bandwidth:</strong> ${data.bandwidth}</div>
                                <div class="data-item"><strong>Data Length:</strong> ${data.data_length}</div>
                                <div class="data-item"><strong>Timestamp:</strong> ${data.timestamp ? data.timestamp.split('T')[1].split('.')[0] : 'N/A'}</div>
                                <div class="data-item"><strong>Time Passed:</strong> ${data.time_passed ? formatTimePassed(data.time_passed) : 'N/A'}</div>
                                <div class="data-item"><strong>SC1 Value:</strong> ${data.subcarrier_1 ? data.subcarrier_1.toFixed(2) : 'N/A'}</div>
                                <div class="data-item"><strong>SC5 Value:</strong> ${data.subcarrier_5 ? data.subcarrier_5.toFixed(2) : 'N/A'}</div>
                                <div class="data-item"><strong>SC9 Value:</strong> ${data.subcarrier_9 ? data.subcarrier_9.toFixed(2) : 'N/A'}</div>
                                <div class="data-item"><strong>SC13 Value:</strong> ${data.subcarrier_13 ? data.subcarrier_13.toFixed(2) : 'N/A'}</div>
                            `;
                        }
                    });
            }
            
            function updateDataLog() {
                fetch('/api/recent')
                    .then(response => response.json())
                    .then(data => {
                        const logDiv = document.getElementById('data-log');
                        if (data.length > 0) {
                            logDiv.innerHTML = data.slice(-15).reverse().map(packet => 
                                `<div>Packet #${packet.packet_num}: Value=${packet.subcarrier_1 ? packet.subcarrier_1.toFixed(2) : 'N/A'}, Time=${packet.time_passed ? formatTimePassed(packet.time_passed) : 'N/A'} [${packet.timestamp ? packet.timestamp.split('T')[1].split('.')[0] : 'N/A'}]</div>`
                            ).join('');
                            logDiv.scrollTop = 0;
                        }
                    });
            
            // also fetch raw serial lines for debugging
            fetch('/api/raw')
                .then(response => response.json())
                .then(lines => {
                    if (lines && lines.length) {
                        console.log('raw lines:', lines.slice(-10));
                    }
                });
            }
            
            function connect() {
                const port = document.getElementById('port-input').value || 'COM5';
                fetch('/api/connect', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({port: port})
                }).then(response => response.json())
                  .then(data => {
                      console.log('connect response', data);
                      // Update available subcarriers after connection
                      setTimeout(initializeSubcarrierDropdowns, 2000);
                      // refresh status right away
                      updateStatus();
                  });
            }
            
            function disconnect() {
                fetch('/api/disconnect', {method: 'POST'});
            }
            
            function startLogging() {
                fetch('/api/start', {method: 'POST'});
            }
            
            function stopLogging() {
                fetch('/api/stop', {method: 'POST'});
            }
            
            // Initialize plot configuration and dropdowns
            initializeSubcarrierDropdowns();
            updatePlotConfig();
            
            // Update every second
            setInterval(() => {
                updateStatus();
                updateLatestData();
                updateDataLog();
                updateCharts();
            }, 1000);
            
            // Initial update
            updateStatus();
        </script>
    </body>
    </html>
    '''

# flask stuff

@app.route('/api/status')
def api_status():
    if logger:
        return jsonify(logger.get_status())
    return jsonify({'connected': False, 'logging': False, 'packet_count': 0, 'port': '', 'session_id': None, 'session_dir': None})

@app.route('/api/latest')
def api_latest():
    if logger:
        return jsonify(logger.get_latest_packet())
    return jsonify({})

@app.route('/api/recent')
def api_recent():
    if logger:
        return jsonify(logger.get_recent_data())
    return jsonify([])

@app.route('/api/subcarriers')
def api_subcarriers():
    if logger:
        return jsonify(logger.get_available_subcarriers())
    return jsonify([])

@app.route('/api/plot_data')
def api_plot_data():
    if logger:
        # Get parameters from query string
        subcarriers_param = request.args.get('subcarriers', '1,5,9,13')
        
        try:
            selected_subcarriers = [int(x.strip()) for x in subcarriers_param.split(',') if x.strip()]
            print(f"Plot data requested for subcarriers: {selected_subcarriers}")  # Debug log
            
            # Validate subcarrier indices
            selected_subcarriers = [sc for sc in selected_subcarriers if 0 <= sc < 128]
            
            if not selected_subcarriers:
                selected_subcarriers = [1, 5, 9, 13]  # Default fallback
                
        except ValueError:
            selected_subcarriers = [1, 5, 9, 13]  # Default fallback
        
        plot_data = logger.get_plot_data(selected_subcarriers)
        print(f"Returning plot data: {plot_data}")  # Debug log
        return jsonify(plot_data)
    return jsonify({'time': [], 'rssi': [], 'subcarriers': {}})

@app.route('/api/raw')
def api_raw():
    if logger:
        raw_data = logger.get_raw_lines()
        # Show detailed info about raw lines
        print(f"[DEBUG] Raw lines count: {len(raw_data)}")
        if raw_data:
            print(f"[DEBUG] Last raw line: {raw_data[-1][:200]}")
        return jsonify(raw_data)
    return jsonify([])

@app.route('/api/connect', methods=['POST'])
def api_connect():
    global logger
    data = request.get_json()
    port = data.get('port', 'COM5')
    print(f"\n{'='*60}")
    print(f"[API] api_connect called with port={port}")
    print(f"{'='*60}\n")
    
    # Close existing connection
    if logger:
        logger.close()
        time.sleep(1)  # Give extra time for cleanup
    
    logger = CSIDataLogger(port)
    success = logger.connect()
    print(f"[API] Connected={success}, port={port}")
    
    # If connected successfully, start logging immediately
    started = False
    if success:
        try:
            started = logger.start_logging()
            print(f"[API] Logging started={started}")
        except Exception as e:
            print(f"[API] Error starting logging: {e}")
            import traceback
            traceback.print_exc()
    
    print(f"[API] Final status: connected={success}, logging={started}\n")
    return jsonify({'success': success, 'port': port, 'logging': started})

@app.route('/api/disconnect', methods=['POST'])
def api_disconnect():
    global logger
    if logger:
        logger.close()
        logger = None
    return jsonify({'success': True})

@app.route('/api/start', methods=['POST'])
def api_start():
    if logger:
        success = logger.start_logging()
        return jsonify({'success': success})
    return jsonify({'success': False, 'error': 'Not connected'})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    if logger:
        logger.stop_logging()
        return jsonify({'success': True})
    return jsonify({'success': False})

if __name__ == '__main__':
    try:
        app.run(debug=True, host='0.0.0.0', port=5000)
    finally:
        if logger:
            logger.close()