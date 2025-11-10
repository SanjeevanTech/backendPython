# Bus Passenger Tracking System - Python Backend

Face recognition and passenger tracking server with ESP32 integration.

## Prerequisites

- Python 3.8+
- MongoDB
- Required Python packages (see requirements.txt)

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

Update MongoDB connection in `simplified_bus_server.py` or use environment variables.

## Running

```bash
python simplified_bus_server.py
```

## Features

- Face recognition and matching
- Season ticket validation
- ESP32 device integration
- Real-time passenger tracking
- Route detection
- Dynamic schedule management

## Scripts

- `simplified_bus_server.py` - Main tracking server
- `route_detector.py` - Route detection logic
- `add_bus_routes_with_stops.py` - Add bus routes
- `register_boards.py` - Register ESP32 boards

## License

ISC
