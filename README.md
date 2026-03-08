# CSI Data Collection Using ESP32

**Course:** ELE529E Embedded Systems  
**Objective:** Real-time CSI data collection and visualization using ESP32 and FreeRTOS

## Features
- Real-time CSI data collection using ESP32
- FreeRTOS-based task management
- Web-based real-time visualization
- Configurable subcarrier selection
- Data logging and export functionality

## Project Structure
```
├── main/
│   ├── main.c              # ESP32 firmware with FreeRTOS implementation
│   └── CMakeLists.txt
├── web_app.py             # Python web application for visualization
├── CMakeLists.txt
└── README.md
```

## Hardware Requirements
- ESP32 Development Board
- USB-to-Serial Cable
- Computer for running web visualization
- Wi-Fi Access Point

## Software Requirements
- ESP-IDF v5.4
- Python 3.x with Flask
- Web Browser (Chrome recommended)

## Setup Instructions

### ESP32 Setup
1. Install ESP-IDF v5.4
2. Clone this repository
3. Configure your WiFi credentials in `sdkconfig`
```bash
# ESP32 CSI Collection Tool Config
CONFIG_SSID="myssid"
CONFIG_PASSWORD="mypassword"
# end of ESP32 CSI Collection Tool Config
```
4. Running the Project - ESP-IDF VS Code Configuration
  - Command Palette (CTRL+Shift+P) > Select the current ESP-IDF version.
  - The "Add VS CODE Configuration Folder" option will be selected.
  
5. Build and flash the firmware:
```bash
idf.py build
idf.py -p (PORT) flash
```
or in VS Code, Command+Shift+P > Select 'ESP-IDF: Build Project' > Flash
! DO NOT monitor as doing so will block web application's access to serial port

### Web Application Setup
1. Install Python dependencies:
```bash
pip install flask pyserial
```
2. Run the web application:
```bash
python web_app.py
```
3. Open your browser and navigate to `http://localhost:5000`

![image](https://github.com/user-attachments/assets/99b9bce7-e8c1-41d6-bc94-7a494d9b1ea7)



## FreeRTOS Implementation
The project uses FreeRTOS for efficient task management:
- WiFi Initialization Task (Priority: 5)
- CSI Processing Task (Priority: 3)
- Inter-task communication using queues
- Task synchronization using semaphores

## Usage
1. Connect to ESP32 through the web interface
2. Start CSI data collection
3. Select subcarriers to plot
4. Observe real-time CSI data visualization
5. Export data for analysis

## Troubleshooting
- If seeing squiggly lines, clean the build, then build again till succeeds. This usually solves ESP-IDF library problems.
- If there are include errors related to cmake, disable the "cmake tools" extension on VS Code.
- If encounter any other problem: Delete the build folder completely, then rebuild
- Make sure ESP32 is connected to an access point. The ESP32 does not work in monitor mode in this project, it needs to be connected.

## Useful Resources
- [ESP-IDF Documentation](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/)
- [ESP32 CSI Documentation](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-guides/wifi.html#wi-fi-channel-state-information)
- [FreeRTOS Documentation](https://www.freertos.org/Documentation/RTOS_book.html)
- [How to create your First ESP IDF project](https://www.youtube.com/watch?v=oHHOCdmLiII)  
- https://github.com/espressif/esp-csi  
- [Getting Started with ESP32 Wireless Networking in C](https://www.youtube.com/watch?v=_dRrarmQiAM)

## Contributing
Feel free to submit issues and enhancement requests!

---
