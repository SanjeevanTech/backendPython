#!/usr/bin/env python3
"""
Dynamic Schedule Management System
Admin can configure bus schedules through web interface
No hard-coded times - fully configurable
"""

import threading
from datetime import datetime, timedelta
from pymongo import MongoClient
import schedule

class DynamicScheduleManager:
    def __init__(self, mongo_url="mongodb+srv://sanjeeBusPassenger:Hz3czXqVoc4ThTiO@buspassenger.lskaqo5.mongodb.net/?retryWrites=true&w=majority&appName=BusPassenger"):
        self.mongo_url = mongo_url
        self.client = None
        self.db = None
        
        # Collections
        self.bus_schedules = None
        
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
    
    
    def load_schedule(self):
        """Load active schedule from database"""
        try:
            # Find active schedule for this bus
            schedule_doc = self.bus_schedules.find_one({
                "bus_id": self.bus_id,
                "active": True
            })
            
            if not schedule_doc:
                print(f"[WARN] No active schedule found in MongoDB for {self.bus_id}. Waiting for Admin to create one.", flush=True)
                self.current_schedule = None
                return None
            
            self.current_schedule = schedule_doc
            print(f"[OK] Loaded Admin Schedule: {schedule_doc.get('schedule_name', 'Unnamed Schedule')}", flush=True)
            
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
            
            # Schedule trip to start EVERY DAY at the boarding time
            schedule.every().day.at(boarding_time).do(
                self.start_trip, direction, trip_name
            )
            
            # Schedule trip to end EVERY DAY at the calculated end time
            schedule.every().day.at(end_time).do(
                self.end_trip, direction, trip_name
            )
            
            print(f"    Scheduled Daily: Start {boarding_time} -> End {end_time} ({trip_name})", flush=True)
        
        print("[OK] Dynamic scheduler configured", flush=True)
    
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
                
                # Always add to windows - we ignore the day of the week
                trip_windows.append({
                    "start_time": start_time_str,
                    "end_time": end_time_str,
                    "route": trip.get('route', trip.get('trip_name', 'Trip')),
                    "active": True,
                    "is_overnight": is_overnight
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

        print("[STOP] Scheduler stopped", flush=True)
