#!/usr/bin/env python3
"""
Register boards in database so they always show (even when offline)
"""

from pymongo import MongoClient

MONGODB_URI = 'mongodb+srv://sanjeeBusPassenger:Hz3czXqVoc4ThTiO@buspassenger.lskaqo5.mongodb.net/bus_passenger_db?retryWrites=true&w=majority&appName=BusPassenger'

# Define your boards
BOARDS = [
    {
        "device_id": "ESP32_CAM_ENTRANCE_001",
        "location": "ENTRY",
        "ip_address": "No IP",
        "last_seen": None  # Will show as OFFLINE until heartbeat received
    },
    {
        "device_id": "ESP32_CAM_EXIT_001",
        "location": "EXIT",
        "ip_address": "No IP",
        "last_seen": None  # Will show as OFFLINE until heartbeat received
    }
]

BUS_ID = "BUS_JC_001"

def register_boards():
    try:
        client = MongoClient(MONGODB_URI)
        db = client['bus_passenger_db']
        power_configs = db['powerConfigs']
        
        print(f"📋 Registering boards for {BUS_ID}...")
        print()
        
        # Get current config
        config = power_configs.find_one({"bus_id": BUS_ID})
        
        if not config:
            print(f"❌ Bus {BUS_ID} not found in database")
            return
        
        existing_boards = config.get('boards', [])
        existing_ids = {b.get('device_id') for b in existing_boards if isinstance(b, dict)}
        
        # Add new boards
        updated_boards = list(existing_boards)
        
        for board in BOARDS:
            if board['device_id'] in existing_ids:
                print(f"✓ {board['device_id']} already registered")
            else:
                updated_boards.append(board)
                print(f"✅ Registered {board['device_id']} - will show as OFFLINE")
        
        # Update database
        power_configs.update_one(
            {"bus_id": BUS_ID},
            {"$set": {"boards": updated_boards}}
        )
        
        print()
        print(f"✅ Total boards: {len(updated_boards)}")
        print()
        print("📊 Power Management will now show:")
        for board in updated_boards:
            device_id = board.get('device_id', 'Unknown')
            location = board.get('location', 'Unknown')
            last_seen = board.get('last_seen')
            status = "● ONLINE" if last_seen else "○ OFFLINE"
            print(f"   {device_id} - {location} - {status}")
        
        print()
        print("💡 When ESP32 powers on and sends heartbeat:")
        print("   ○ OFFLINE → ● ONLINE")
        print()
        print("💡 When ESP32 powers off (>75 seconds no heartbeat):")
        print("   ● ONLINE → ○ OFFLINE")
        
        client.close()
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    register_boards()
