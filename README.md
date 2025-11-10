# Bus Passenger Tracking System - Python Backend

Face recognition and passenger tracking server with ESP32 integration.

## Prerequisites

- Python 3.8+
- MongoDB
- Required Python packages (see requirements.txt)

## Local Installation

```bash
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and update:
```bash
MONGODB_URI=your_mongodb_connection_string
PORT=8888
```

## Running Locally

```bash
python simplified_bus_server.py
```

## Deploy to Railway

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new)

1. Click the button above or go to https://railway.app
2. Sign in with GitHub
3. Select this repository
4. Add environment variables:
   - `MONGODB_URI` (your MongoDB connection string)
5. Deploy!

Railway will automatically:
- Install all dependencies (including dlib)
- Set the PORT environment variable
- Start the server

## API Endpoints

- `GET /status` - Server status
- `POST /face-log` - Process face detection from ESP32
- `GET /passengers` - Get all passengers
- `GET /trip` - Current trip status
- `POST /trip/start` - Start new trip
- `POST /trip/end` - End current trip
- `GET /api/schedule` - Get schedule
- `GET /api/power-config` - Power management config

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
