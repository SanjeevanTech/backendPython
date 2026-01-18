#!/usr/bin/env python3
"""
Dynamic Schedule Management System
Admin can configure bus schedules through web interface
No hard-coded times - fully configurable
"""

import json
import threading
from datetime import datetime, timedelta, time as dt_time
from pymongo import MongoClient
import schedule

class DynamicScheduleManager:
    def __init__(self, mongo_url="mongodb+srv://sanjeeBusPassenger:Hz3czXqVoc4ThTiO@buspassenger.lskaqo5.mongodb.net/?retryWrites=true&w=majority&appName=BusPassenger"):
        self.mongo_url = mongo_url
        self.client = None
        self.db = None
        
        # Collections
        self.bus_schedules = None
        self.active_trips = None
        
        # Current schedule (loaded from database)
        self.current_schedule = None
        self.bus_id = "BUS_JC_001"
        
        # Scheduler state
        self.scheduler_running = False
        self.scheduler_thread = None
        
        # Callbacks
        self.on_trip_start = None
        self.on_trip_end = None
        
        self.init_database()
        self.load_schedule()
    
    def init_database(self):
        """Initialize MongoDB connection"""
        try:
            self.client = MongoClient(self.mongo_url)
            self.db = self.client['bus_passenger_db']
            
            # Collections
            self.bus_schedules = self.db['bus_schedules']
            self.trip_sessions = self.db['tripSessions']
            
            # Create indexes
            self.bus_schedules.create_index([("bus_id", 1), ("active", 1)])
            
            print("[OK] Dynamic Schedule Manager initialized", flush=True)
            
        except Exception as e:
            print(f"[ERROR] Database connection failed: {e}", flush=True)
            raise
    
    def create_default_schedule(self):
        """Create default schedule if none exists"""
        default_schedule = {
            "bus_id": self.bus_id,
            "route_name": "Jaffna-Colombo",
            "schedule_name": "Default Daily Schedule",
            "active": True,
            "trips": [
                {
                    "trip_name": "Morning - Jaffna to Colombo",
                    "route": "Jaffna-Colombo",
                    "direction": "jaffna_to_colombo",
                    "boarding_start_time": "06:00",
                    "departure_time": "07:00",
                    "estimated_arrival_time": "17:00",
                    "stop_duration_minutes": 30,
                    "days_of_week": ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"],
                    "active": True
                },
                {
                    "trip_name": "Evening - Colombo to Jaffna",
                    "route": "Colombo-Jaffna",
                    "direction": "colombo_to_jaffna",
                    "boarding_start_time": "17:30",
                    "departure_time": "18:00",
                    "estimated_arrival_time": "03:00",  # Next day
                    "stop_duration_minutes": 30,
                    "days_of_week": ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"],
                    "active": True
                }
            ],
            "timezone": "Asia/Colombo",
            "auto_power_management": True,
            "power_off_delay_minutes": 30,  # Power off 30 minutes after trip end
            "created_at": datetime.now(),
            "updated_at": datetime.now(),
            "created_by": "system"
        }
        
        # Insert or update default schedule
        # Use update_one with upsert=True to avoid duplicate key errors if it already exists but was not found by filter
        self.bus_schedules.update_one(
            {"bus_id": self.bus_id},
            {"$set": default_schedule},
            upsert=True
        )
        # Fetch the inserted/updated document
        saved_schedule = self.bus_schedules.find_one({"bus_id": self.bus_id})
        print(f"[OK] Created/Updated default schedule: {saved_schedule.get('_id')}", flush=True)
        return saved_schedule
    
    def load_schedule(self):
        """Load active schedule from database"""
        try:
            # Find active schedule for this bus
            schedule_doc = self.bus_schedules.find_one({
                "bus_id": self.bus_id,
                "active": True
            })
            
            if not schedule_doc:
                print("[WARN] No active schedule found, creating default...", flush=True)
                schedule_doc = self.create_default_schedule()
            
            self.current_schedule = schedule_doc
            print(f"[OK] Loaded schedule: {schedule_doc.get('schedule_name', 'Unnamed Schedule')}", flush=True)
            
            # Display current schedule
            self.display_current_schedule()
            
            return schedule_doc
            
        except Exception as e:
            print(f"[ERROR] Error loading schedule: {e}", flush=True)
            return None
    
    def display_current_schedule(self):
        """Display current schedule in readable format"""
        if not self.current_schedule:
            print("[ERROR] No schedule loaded", flush=True)
            return
        
        print(f"\n Current Schedule: {self.current_schedule.get('schedule_name', 'Unnamed Schedule')}", flush=True)
        print(f"[BUS] Bus: {self.current_schedule['bus_id']} - {self.current_schedule.get('route_name', 'Unknown Route')}", flush=True)
        print(f" Timezone: {self.current_schedule.get('timezone', 'Asia/Colombo')}", flush=True)
        print(f" Auto Power Management: {self.current_schedule.get('auto_power_management', False)}", flush=True)
        
        for i, trip in enumerate(self.current_schedule.get('trips', []), 1):
            if trip.get('active', True):
                print(f"\n[BUS] Trip {i}: {trip.get('trip_name', 'Unnamed Trip')}", flush=True)
                print(f"   [LOC] Direction: {trip.get('direction', 'Unknown')}", flush=True)
                print(f"   [DOOR] Boarding: {trip.get('boarding_start_time', 'N/A')}", flush=True)
                print(f"   [SPEED] Departure: {trip.get('departure_time', 'N/A')}", flush=True)
                print(f"    Arrival: {trip.get('estimated_arrival_time', 'N/A')}", flush=True)
                print(f"   [TIME] Stop Duration: {trip.get('stop_duration_minutes', 5)} minutes", flush=True)
                print(f"    Days: {', '.join(trip.get('days_of_week', ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']))}", flush=True)
    
    def update_schedule(self, schedule_data, updated_by="admin"):
        """Update schedule configuration"""
        try:
            # Add metadata
            schedule_data["updated_at"] = datetime.now()
            schedule_data["updated_by"] = updated_by
            
            # Update in database
            result = self.bus_schedules.update_one(
                {"bus_id": self.bus_id, "active": True},
                {"$set": schedule_data}
            )
            
            if result.modified_count > 0:
                # Reload schedule
                self.load_schedule()
                
                # Restart scheduler with new times
                self.restart_scheduler()
                
                print(f"[OK] Schedule updated successfully by {updated_by}", flush=True)
                return True
            else:
                print("[ERROR] Failed to update schedule", flush=True)
                return False
                
        except Exception as e:
            print(f"[ERROR] Error updating schedule: {e}", flush=True)
            return False
    
    def setup_dynamic_scheduler(self):
        """Setup scheduler based on current schedule configuration"""
        if not self.current_schedule:
            print("[ERROR] No schedule loaded, cannot setup scheduler", flush=True)
            return
        
        # Clear existing scheduled jobs
        schedule.clear()
        
        print("[SYNC] Setting up dynamic scheduler...", flush=True)
        
        for trip in self.current_schedule.get('trips', []):
            if not trip.get('active', True):
                continue
            
            trip_name = trip.get('trip_name', 'Unnamed Trip')
            direction = trip.get('direction', 'Unknown')
            boarding_time = trip.get('boarding_start_time')
            
            # Skip if no boarding time
            if not boarding_time:
                print(f"[WARN] Skipping trip '{trip_name}' - no boarding time", flush=True)
                continue
            
            # Calculate trip end time (arrival + stop duration)
            arrival_time = trip.get('estimated_arrival_time', boarding_time)
            stop_minutes = trip.get('stop_duration_minutes', 5)  # Default 5 minutes if not specified
            end_time = self.calculate_end_time(arrival_time, stop_minutes)
            
            # Schedule for each day of the week
            days_of_week = trip.get('days_of_week', ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'])
            for day_name in days_of_week:
                # Schedule trip start
                getattr(schedule.every(), day_name).at(boarding_time).do(
                    self.start_trip, direction, trip_name
                )
                
                # Handle overnight trips: If end_time is earlier than boarding_time, it ends the next day
                end_day_name = day_name
                if end_time < boarding_time:
                    end_day_name = self.get_next_day_name(day_name)
                    print(f"    [OVERNIGHT] Trip ends next day: {end_day_name} at {end_time}", flush=True)
                
                # Schedule trip end
                getattr(schedule.every(), end_day_name).at(end_time).do(
                    self.end_trip, direction, trip_name
                )
                
                print(f"    {day_name.capitalize()}: Start {boarding_time} -> End {end_day_name.capitalize()} {end_time} ({trip_name})", flush=True)
        
        print("[OK] Dynamic scheduler configured", flush=True)
    
    def calculate_end_time(self, arrival_time, stop_minutes):
        """Calculate trip end time"""
        try:
            arrival = datetime.strptime(arrival_time, "%H:%M")
            end_time = arrival + timedelta(minutes=stop_minutes)
            return end_time.strftime("%H:%M")
        except:
            return "18:00"  # Default fallback

    def get_next_day_name(self, day_name):
        """Get the name of the next day of the week"""
        days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
        try:
            idx = days.index(day_name.lower())
            return days[(idx + 1) % 7]
        except ValueError:
            return day_name
    
    def start_trip(self, direction, trip_name):
        """Start trip automatically"""
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M')
            trip_id = f"TRIP_{self.bus_id}_{direction.upper()}_{timestamp}"
            
            # Create trip record
            trip_data = {
                "trip_id": trip_id,
                "bus_id": self.bus_id,
                "direction": direction,
                "trip_name": trip_name,
                "status": "active",
                "start_time": datetime.now(),
                "schedule_config": self.current_schedule,
                "created_at": datetime.now()
            }
            
            self.trip_sessions.insert_one(trip_data)
            
            print(f"\n[SPEED] AUTO-STARTED: {trip_name}", flush=True)
            print(f"   Trip ID: {trip_id}", flush=True)
            print(f"   Direction: {direction}", flush=True)
            print(f"   Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
            
            # Trigger callback
            if self.on_trip_start:
                try:
                    self.on_trip_start(self.bus_id, trip_id, direction)
                except Exception as cb_e:
                    print(f"[ERROR] Error in on_trip_start callback: {cb_e}", flush=True)
            
        except Exception as e:
            print(f"[ERROR] Error starting trip: {e}", flush=True)
    
    def end_trip(self, direction, trip_name):
        """End trip automatically"""
        try:
            active_trip = self.trip_sessions.find_one({
                "bus_id": self.bus_id,
                "status": "active",
                "direction": direction
            })
            
            if active_trip:
                # Update trip status
                self.trip_sessions.update_one(
                    {"_id": active_trip["_id"]},
                    {
                        "$set": {
                            "status": "completed",
                            "end_time": datetime.now(),
                            "updated_at": datetime.now()
                        }
                    }
                )
                
                print(f"\n AUTO-ENDED: {trip_name}", flush=True)
                print(f"   Trip ID: {active_trip['trip_id']}", flush=True)
                print(f"   Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
                
                # Trigger callback
                if self.on_trip_end:
                    try:
                        self.on_trip_end(self.bus_id)
                    except Exception as cb_e:
                        print(f"[ERROR] Error in on_trip_end callback: {cb_e}", flush=True)
            
        except Exception as e:
            print(f"[ERROR] Error ending trip: {e}", flush=True)
    
    def restart_scheduler(self):
        """Restart scheduler with new configuration"""
        print("[SYNC] Restarting scheduler with new configuration...", flush=True)
        self.setup_dynamic_scheduler()
    
    def get_current_schedule_json(self):
        """Get current schedule as JSON for API"""
        if not self.current_schedule:
            return {"error": "No schedule loaded"}
        
        # Remove MongoDB ObjectId for JSON serialization
        schedule_copy = dict(self.current_schedule)
        if '_id' in schedule_copy:
            schedule_copy['_id'] = str(schedule_copy['_id'])
        
        return schedule_copy
    
    def get_todays_trip_windows(self, bus_id=None):
        """Get trip windows for today for power management (Fresh from DB)"""
        target_bus = bus_id if bus_id else self.bus_id
        
        # Always fetch fresh from DB to avoid staleness
        schedule_doc = self.bus_schedules.find_one({
            "bus_id": target_bus,
            "active": True
        })
        
        if not schedule_doc:
            return []
            
        trip_windows = []
        now = datetime.now()
        today_name = now.strftime('%A').lower()
        yesterday_name = (now - timedelta(days=1)).strftime('%A').lower()
        
        print(f"   [SYNC] Checking windows for today ({today_name}) and yesterday ({yesterday_name})", flush=True)
        
        for trip in schedule_doc.get('trips', []):
            if not trip.get('active', True):
                continue
                
            days = [d.lower() for d in trip.get('days_of_week', [])]
            
            # Calculate times
            try:
                start_time_str = trip.get('boarding_start_time', '06:00')
                arrival_time_str = trip.get('estimated_arrival_time', start_time_str)
                stop_minutes = trip.get('stop_duration_minutes', 30)
                
                # Calculate end_time_str
                arrival_dt = datetime.strptime(arrival_time_str, "%H:%M")
                end_dt = arrival_dt + timedelta(minutes=stop_minutes)
                end_time_str = end_dt.strftime("%H:%M")
                
                is_overnight = end_time_str < start_time_str
                
                # Add if trip starts today OR if it started yesterday and is overnight
                if today_name in days or (yesterday_name in days and is_overnight):
                    trip_windows.append({
                        "start_time": start_time_str,
                        "end_time": end_time_str,
                        "route": trip.get('route', trip.get('trip_name', 'Trip')),
                        "active": True,
                        "is_overnight": is_overnight,
                        "started_yesterday": yesterday_name in days and today_name not in days
                    })
            except Exception as e:
                print(f"[ERROR] Error parsing trip window: {e}", flush=True)
                continue
                
        return trip_windows
    
    def run_scheduler(self):
        """Run the scheduler continuously"""
        self.scheduler_running = True
        print("[SYNC] Dynamic scheduler started...", flush=True)
        
        while self.scheduler_running:
            schedule.run_pending()
            threading.Event().wait(60)  # Check every minute
    
    def start_scheduler_thread(self):
        """Start scheduler in background thread"""
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            print("[WARN] Scheduler already running", flush=True)
            return
        
        self.setup_dynamic_scheduler()
        self.scheduler_thread = threading.Thread(target=self.run_scheduler, daemon=True)
        self.scheduler_thread.start()
        print("[OK] Scheduler thread started", flush=True)
    
    def stop_scheduler(self):
        """Stop the scheduler"""
        self.scheduler_running = False
        if self.scheduler_thread:
            self.scheduler_thread.join(timeout=5)
        print("[STOP] Scheduler stopped", flush=True)

# Example usage and API integration
def create_schedule_api_endpoints():
    """Example API endpoints for schedule management"""
    
    schedule_manager = DynamicScheduleManager()
    
    def get_schedule():
        """GET /api/schedule - Get current schedule"""
        return schedule_manager.get_current_schedule_json()
    
    def update_schedule(new_schedule_data, admin_user="admin"):
        """POST /api/schedule - Update schedule"""
        success = schedule_manager.update_schedule(new_schedule_data, admin_user)
        if success:
            return {"status": "success", "message": "Schedule updated successfully"}
        else:
            return {"status": "error", "message": "Failed to update schedule"}
    
    def get_schedule_status():
        """GET /api/schedule/status - Get scheduler status"""
        return {
            "scheduler_running": schedule_manager.scheduler_running,
            "current_schedule_name": schedule_manager.current_schedule.get('schedule_name') if schedule_manager.current_schedule else None,
            "bus_id": schedule_manager.bus_id,
            "last_updated": schedule_manager.current_schedule.get('updated_at') if schedule_manager.current_schedule else None
        }
    
    return schedule_manager, {
        "get_schedule": get_schedule,
        "update_schedule": update_schedule,
        "get_status": get_schedule_status
    }

if __name__ == "__main__":
    # Test the dynamic schedule manager
    schedule_manager = DynamicScheduleManager()
    schedule_manager.start_scheduler_thread()
    
    print("\n Dynamic Schedule Manager Commands:", flush=True)
    print("  status  - Show current schedule", flush=True)
    print("  reload  - Reload schedule from database", flush=True)
    print("  quit    - Exit", flush=True)
    
    while True:
        try:
            command = input("\n> ").strip().lower()
            
            if command == "status":
                schedule_manager.display_current_schedule()
            
            elif command == "reload":
                schedule_manager.load_schedule()
                schedule_manager.restart_scheduler()
            
            elif command == "quit":
                schedule_manager.stop_scheduler()
                print(" Goodbye!", flush=True)
                break
            
            else:
                print("Unknown command", flush=True)
                
        except KeyboardInterrupt:
            schedule_manager.stop_scheduler()
            print("\n Goodbye!", flush=True)
            break