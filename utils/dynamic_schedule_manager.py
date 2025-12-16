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
        
        self.init_database()
        self.load_schedule()
    
    def init_database(self):
        """Initialize MongoDB connection"""
        try:
            self.client = MongoClient(self.mongo_url)
            self.db = self.client['bus_passenger_db']
            
            # Collections
            self.bus_schedules = self.db['bus_schedules']
            self.active_trips = self.db['active_trips']
            
            # Create indexes
            self.bus_schedules.create_index([("bus_id", 1), ("active", 1)])
            
            print("✅ Dynamic Schedule Manager initialized")
            
        except Exception as e:
            print(f"❌ Database connection failed: {e}")
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
        print(f"✅ Created/Updated default schedule: {saved_schedule.get('_id')}")
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
                print("⚠️ No active schedule found, creating default...")
                schedule_doc = self.create_default_schedule()
            
            self.current_schedule = schedule_doc
            print(f"✅ Loaded schedule: {schedule_doc.get('schedule_name', 'Unnamed Schedule')}")
            
            # Display current schedule
            self.display_current_schedule()
            
            return schedule_doc
            
        except Exception as e:
            print(f"❌ Error loading schedule: {e}")
            return None
    
    def display_current_schedule(self):
        """Display current schedule in readable format"""
        if not self.current_schedule:
            print("❌ No schedule loaded")
            return
        
        print(f"\n📅 Current Schedule: {self.current_schedule.get('schedule_name', 'Unnamed Schedule')}")
        print(f"🚌 Bus: {self.current_schedule['bus_id']} - {self.current_schedule.get('route_name', 'Unknown Route')}")
        print(f"🌍 Timezone: {self.current_schedule.get('timezone', 'Asia/Colombo')}")
        print(f"⚡ Auto Power Management: {self.current_schedule.get('auto_power_management', False)}")
        
        for i, trip in enumerate(self.current_schedule.get('trips', []), 1):
            if trip.get('active', True):
                print(f"\n🚌 Trip {i}: {trip.get('trip_name', 'Unnamed Trip')}")
                print(f"   📍 Direction: {trip.get('direction', 'Unknown')}")
                print(f"   🚪 Boarding: {trip.get('boarding_start_time', 'N/A')}")
                print(f"   🚀 Departure: {trip.get('departure_time', 'N/A')}")
                print(f"   🏁 Arrival: {trip.get('estimated_arrival_time', 'N/A')}")
                print(f"   ⏱️ Stop Duration: {trip.get('stop_duration_minutes', 5)} minutes")
                print(f"   📆 Days: {', '.join(trip.get('days_of_week', ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']))}")
    
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
                
                print(f"✅ Schedule updated successfully by {updated_by}")
                return True
            else:
                print("❌ Failed to update schedule")
                return False
                
        except Exception as e:
            print(f"❌ Error updating schedule: {e}")
            return False
    
    def setup_dynamic_scheduler(self):
        """Setup scheduler based on current schedule configuration"""
        if not self.current_schedule:
            print("❌ No schedule loaded, cannot setup scheduler")
            return
        
        # Clear existing scheduled jobs
        schedule.clear()
        
        print("🔄 Setting up dynamic scheduler...")
        
        for trip in self.current_schedule.get('trips', []):
            if not trip.get('active', True):
                continue
            
            trip_name = trip.get('trip_name', 'Unnamed Trip')
            direction = trip.get('direction', 'Unknown')
            boarding_time = trip.get('boarding_start_time')
            
            # Skip if no boarding time
            if not boarding_time:
                print(f"⚠️ Skipping trip '{trip_name}' - no boarding time")
                continue
            
            # Calculate trip end time (arrival + stop duration)
            arrival_time = trip.get('estimated_arrival_time', boarding_time)
            stop_minutes = trip.get('stop_duration_minutes', 5)  # Default 5 minutes if not specified
            end_time = self.calculate_end_time(arrival_time, stop_minutes)
            
            # Schedule for each day of the week
            days_of_week = trip.get('days_of_week', ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'])
            for day in days_of_week:
                # Schedule trip start
                getattr(schedule.every(), day).at(boarding_time).do(
                    self.start_trip, direction, trip_name
                )
                
                # Schedule trip end
                getattr(schedule.every(), day).at(end_time).do(
                    self.end_trip, direction, trip_name
                )
                
                print(f"   📅 {day.capitalize()}: {boarding_time} → {end_time} ({trip_name})")
        
        print("✅ Dynamic scheduler configured")
    
    def calculate_end_time(self, arrival_time, stop_minutes):
        """Calculate trip end time"""
        try:
            arrival = datetime.strptime(arrival_time, "%H:%M")
            end_time = arrival + timedelta(minutes=stop_minutes)
            return end_time.strftime("%H:%M")
        except:
            return "18:00"  # Default fallback
    
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
                "status": "ACTIVE",
                "start_time": datetime.now(),
                "schedule_config": self.current_schedule,
                "created_at": datetime.now()
            }
            
            self.active_trips.insert_one(trip_data)
            
            print(f"\n🚀 AUTO-STARTED: {trip_name}")
            print(f"   Trip ID: {trip_id}")
            print(f"   Direction: {direction}")
            print(f"   Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            
        except Exception as e:
            print(f"❌ Error starting trip: {e}")
    
    def end_trip(self, direction, trip_name):
        """End trip automatically"""
        try:
            # Find active trip
            active_trip = self.active_trips.find_one({
                "bus_id": self.bus_id,
                "status": "ACTIVE",
                "direction": direction
            })
            
            if active_trip:
                # Update trip status
                self.active_trips.update_one(
                    {"_id": active_trip["_id"]},
                    {
                        "$set": {
                            "status": "COMPLETED",
                            "end_time": datetime.now(),
                            "updated_at": datetime.now()
                        }
                    }
                )
                
                print(f"\n🏁 AUTO-ENDED: {trip_name}")
                print(f"   Trip ID: {active_trip['trip_id']}")
                print(f"   Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            
        except Exception as e:
            print(f"❌ Error ending trip: {e}")
    
    def restart_scheduler(self):
        """Restart scheduler with new configuration"""
        print("🔄 Restarting scheduler with new configuration...")
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
    
    def get_todays_trip_windows(self):
        """Get trip windows for today for power management"""
        if not self.current_schedule:
            return []
            
        trip_windows = []
        today_name = datetime.now().strftime('%A').lower()
        
        for trip in self.current_schedule.get('trips', []):
            if not trip.get('active', True):
                continue
                
            days = [d.lower() for d in trip.get('days_of_week', [])]
            if today_name not in days:
                continue
                
            try:
                # Get start time
                start_time = trip.get('boarding_start_time', '06:00')
                start_h, start_m = map(int, start_time.split(':'))
                
                # Calculate end time
                arrival_time = trip.get('estimated_arrival_time', start_time)
                stop_minutes = trip.get('stop_duration_minutes', 30)
                
                # Use same calculation as scheduler
                arrival_dt = datetime.strptime(arrival_time, "%H:%M")
                end_dt = arrival_dt + timedelta(minutes=stop_minutes)
                end_time = end_dt.strftime("%H:%M")
                end_h, end_m = map(int, end_time.split(':'))
                
                trip_windows.append({
                    "start_time": f"{start_h:02d}:{start_m:02d}",
                    "end_time": f"{end_h:02d}:{end_m:02d}",
                    "route": trip.get('trip_name', 'Trip'),
                    "active": True
                })
            except Exception as e:
                print(f"❌ Error parsing trip for power window: {e}")
                continue
                
        return trip_windows
    
    def run_scheduler(self):
        """Run the scheduler continuously"""
        self.scheduler_running = True
        print("🔄 Dynamic scheduler started...")
        
        while self.scheduler_running:
            schedule.run_pending()
            threading.Event().wait(60)  # Check every minute
    
    def start_scheduler_thread(self):
        """Start scheduler in background thread"""
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            print("⚠️ Scheduler already running")
            return
        
        self.setup_dynamic_scheduler()
        self.scheduler_thread = threading.Thread(target=self.run_scheduler, daemon=True)
        self.scheduler_thread.start()
        print("✅ Scheduler thread started")
    
    def stop_scheduler(self):
        """Stop the scheduler"""
        self.scheduler_running = False
        if self.scheduler_thread:
            self.scheduler_thread.join(timeout=5)
        print("🛑 Scheduler stopped")

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
    
    print("\n🎛️ Dynamic Schedule Manager Commands:")
    print("  status  - Show current schedule")
    print("  reload  - Reload schedule from database")
    print("  quit    - Exit")
    
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
                print("👋 Goodbye!")
                break
            
            else:
                print("Unknown command")
                
        except KeyboardInterrupt:
            schedule_manager.stop_scheduler()
            print("\n👋 Goodbye!")
            break