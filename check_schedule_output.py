import sys
import os
import json

# Add current directory to path
sys.path.append(os.getcwd())

from utils.dynamic_schedule_manager import DynamicScheduleManager

try:
    print("🔄 Initializing Schedule Manager...")
    manager = DynamicScheduleManager()
    
    print("\n📅 Checking Today's Trip Windows:")
    windows = manager.get_todays_trip_windows()
    
    print(json.dumps(windows, indent=2))
    
    if not windows:
        print("⚠️ No windows found for today. Check if schedule is active and includes today.")
    else:
        print(f"✅ Found {len(windows)} windows. JSON format check:")
        first = windows[0]
        if 'start_time' in first and 'end_time' in first and 'route' in first:
            print("✅ Format is CORRECT (matches ESP32 expectations)")
        else:
            print("❌ Format is INCORRECT")
            
except Exception as e:
    print(f"❌ Error: {e}")
