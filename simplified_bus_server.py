#!/usr/bin/env python3
"""
Simplified Bus Passenger Tracking Server
- One bus: BUS_JC_001 (Jaffna-Colombo)
- Temporary storage for matching
- Final collection: busPassengerList
- ESP32 Integration for face detection
"""

import os
import sys
import json
import time
import numpy as np
import requests
import math
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from sklearn.metrics.pairwise import cosine_similarity
from pymongo import MongoClient
from bson import ObjectId

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(__file__))

from utils.dynamic_schedule_manager import DynamicScheduleManager
from route_detector import RouteDetector
from face_recognition_helper import extract_face_embedding_from_base64

class SimplifiedBusTracker:
    def __init__(self, mongo_url="mongodb+srv://sanjeeBusPassenger:Hz3czXqVoc4ThTiO@buspassenger.lskaqo5.mongodb.net/?retryWrites=true&w=majority&appName=BusPassenger"):
        self.mongo_url = mongo_url
        self.client = None
        self.db = None
        
        # Collections
        self.temp_entries = None      # Temporary storage for unmatched entries
        self.final_passengers = None  # Final collection: busPassengerList
        self.unmatched_passengers = None  # Unmatched passengers collection
        self.power_configs = None     # Power management configurations per bus
        self.season_ticket_members = None  # Season ticket members collection
        self.contractors = None       # Contractor collection
        
        # Configuration - MULTI-BUS SUPPORT
        self.default_bus_id = "BUS_JC_001"  # Default bus if none specified
        self.route_name = "Jaffna-Colombo"  # Will be updated automatically
        self.similarity_threshold = 0.7
        self.season_ticket_similarity_threshold = 0.65  # Lower threshold for ESP32 face variations
        self.time_window_hours = 48  # Increased to 48 hours for testing
        self.timezone_offset_hours = 5.5  # Adjust for Sri Lanka (+5:30)
        self.debug_allow_all_logs = True  # SET TO TRUE FOR TESTING (Accepts logs outside schedule)
        
        # Trip session management - MULTI-BUS SUPPORT
        self.current_trips = {}  # Dict: bus_id -> current_trip (supports multiple buses)
        self.trip_sessions = None  # Collection to store trip sessions
        
        # Route detection
        self.route_detector = None  # Will be initialized after database connection
        
        self.init_database()
    
    def init_database(self):
        """Initialize MongoDB connection"""
        try:
            self.client = MongoClient(self.mongo_url)
            self.db = self.client['bus_passenger_db']
            
            # Collections
            self.temp_entries = self.db['temp_entries']        # Temporary unmatched entries
            self.final_passengers = self.db['busPassengerList'] # Final matched passengers
            self.unmatched_passengers = self.db['unmatchedPassengers'] # Unmatched passengers
            self.trip_sessions = self.db['tripSessions']       # Trip session tracking
            self.power_configs = self.db['powerConfigs']       # Power management configs per bus
            self.season_ticket_members = self.db['seasonTicketMembers']  # Season ticket members
            self.contractors = self.db['contractors']          # Contractors
            
            # Create indexes
            self.temp_entries.create_index([("bus_id", 1), ("trip_id", 1), ("timestamp", 1)])
            self.final_passengers.create_index([("bus_id", 1), ("trip_id", 1), ("entry_timestamp", 1)])
            self.unmatched_passengers.create_index([("bus_id", 1), ("trip_id", 1), ("timestamp", 1), ("type", 1)])
            self.trip_sessions.create_index([("bus_id", 1), ("trip_id", 1), ("start_time", 1)])
            self.power_configs.create_index([("bus_id", 1)], unique=True)
            self.season_ticket_members.create_index([("member_id", 1)], unique=True)
            self.season_ticket_members.create_index([("is_active", 1), ("valid_from", 1), ("valid_until", 1)])
            self.contractors.create_index([("bus_id", 1)], unique=True)
            
            print("✅ Connected to MongoDB - Multi-Bus Tracking Enabled")
            print(f"🚌 Default Bus: {self.default_bus_id} ({self.route_name})")
            print(f"🔄 Multi-bus: Trips are created per bus_id from ESP32 requests")
            print(f"📊 Collections: temp_entries, busPassengerList, unmatchedPassengers, tripSessions, seasonTicketMembers")
            
            # Initialize route detector
            try:
                self.route_detector = RouteDetector(self.db)
                print("✅ Route detector initialized")
            except Exception as e:
                print(f"⚠️ Route detector initialization failed: {e}")
                self.route_detector = None
            
            # Initialize contractor similarity threshold
            self.contractor_similarity_threshold = 0.75  # Slightly higher for security
            
            # Don't auto-load trip on startup - trips are created on-demand per bus
            
        except Exception as e:
            print(f"❌ Failed to connect to MongoDB: {e}")
            raise
    
    def generate_trip_id(self, start_time=None, bus_id=None):
        """Generate unique trip ID for a specific bus"""
        if start_time is None:
            start_time = datetime.now()
        if bus_id is None:
            bus_id = self.default_bus_id
        date_str = start_time.strftime('%Y-%m-%d')
        time_str = start_time.strftime('%H:%M')
        return f"{bus_id}_{date_str}_{time_str}"
    
    def _parse_timestamp_safe(self, timestamp_str):
        """Safely parse timestamp, handling invalid/epoch timestamps from ESP32"""
        try:
            if not timestamp_str:
                print(f"⚠️ Empty timestamp, using server time")
                return datetime.now()
            
            # Handle timezone format: replace +00:00 with Z for fromisoformat compatibility
            timestamp_str = str(timestamp_str).replace('+00:00', 'Z').replace('Z', '+00:00')
            
            # Try parsing with fromisoformat
            parsed_time = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            
            # Check if timestamp is before 2020 (likely unsynced ESP32 time)
            if parsed_time.year < 2020:
                print(f"⚠️ Invalid timestamp detected (ESP32 time not synced): {timestamp_str}, using server time")
                return datetime.now()
            
            # Remove timezone info to store as naive datetime (MongoDB compatibility)
            if parsed_time.tzinfo is not None:
                parsed_time = parsed_time.replace(tzinfo=None)
            
            print(f"✅ Parsed timestamp: {timestamp_str} → {parsed_time}")
            return parsed_time
        except Exception as e:
            print(f"⚠️ Error parsing timestamp '{timestamp_str}': {e}, using server time")
            return datetime.now()
    
    def load_current_trip(self, bus_id=None):
        """Load active trip from database for specific bus or create new one"""
        if bus_id is None:
            bus_id = self.default_bus_id
        try:
            # Find active trip for this bus
            active_trip = self.trip_sessions.find_one({
                "bus_id": bus_id,
                "status": "active"
            })
            
            if active_trip:
                self.current_trips[bus_id] = {
                    'trip_id': active_trip['trip_id'],
                    'bus_id': bus_id,
                    'route_name': active_trip.get('route_name', self.route_name),
                    'start_time': active_trip['start_time'],
                    'status': 'active',
                    '_id': active_trip['_id']
                }
                print(f"📍 Loaded active trip for {bus_id}: {active_trip['trip_id']}")
            else:
                # Auto-start new trip for this bus
                self.start_new_trip(bus_id=bus_id)
        except Exception as e:
            print(f"❌ Error loading trip for {bus_id}: {e}")
            self.start_new_trip(bus_id=bus_id)
    
    def get_current_trip_for_bus(self, bus_id):
        """Get or create current trip for a specific bus"""
        if bus_id not in self.current_trips:
            self.load_current_trip(bus_id)
        return self.current_trips.get(bus_id)
    
    def start_new_trip(self, start_time=None, initial_gps=None, bus_id=None):
        """Start a new trip session with smart route detection for specific bus"""
        if bus_id is None:
            bus_id = self.default_bus_id
        try:
            if start_time is None:
                start_time = datetime.utcnow()
            
            # End previous trip for this bus if exists
            if bus_id in self.current_trips and self.current_trips[bus_id].get('status') == 'active':
                self.end_current_trip(bus_id=bus_id)
            
            # Cleanup orphaned entries for this bus
            self.cleanup_old_temp_entries(hours_old=0, bus_id=bus_id)
            
            # Generate trip ID
            trip_id = self.generate_trip_id(start_time, bus_id)
            
            # Smart route detection based on GPS
            detected_route = self.route_name  # Default
            if initial_gps and hasattr(self, 'route_detector') and self.route_detector:
                route_info = self.route_detector.detect_route_direction(bus_id, initial_gps, start_time)
                if route_info:
                    detected_route = route_info['route_name']
                    print(f"🛣️ Auto-detected route for {bus_id}: {detected_route}")
            
            # Create trip session record
            trip_session = {
                'trip_id': trip_id,
                'bus_id': bus_id,
                'route_name': detected_route,
                'start_time': start_time,
                'end_time': None,
                'status': 'active',
                'total_passengers': 0,
                'total_unmatched': 0,
                'route_detection_gps': initial_gps,
                'created_at': datetime.utcnow()
            }
            
            result = self.trip_sessions.insert_one(trip_session)
            
            # Store current trip info for this bus
            self.current_trips[bus_id] = {
                'trip_id': trip_id,
                'bus_id': bus_id,
                'route_name': detected_route,
                'start_time': start_time,
                'status': 'active',
                '_id': result.inserted_id
            }
            
            print(f"🚌 Started new trip for {bus_id}: {trip_id}")
            return trip_id
        except Exception as e:
            print(f"❌ Error starting trip for {bus_id}: {e}")
            return None
    
    def end_current_trip(self, bus_id=None):
        """End current trip for specific bus and move unmatched to unmatched collection"""
        if bus_id is None:
            bus_id = self.default_bus_id
        try:
            current_trip = self.current_trips.get(bus_id)
            if not current_trip:
                print(f"❌ No active trip for {bus_id}")
                return False
            
            trip_id = current_trip['trip_id']
            
            # Count passengers for this trip
            passenger_count = self.final_passengers.count_documents({"trip_id": trip_id})
            
            # Move remaining temp_entries to unmatched (ENTRY type - no exit found)
            remaining = list(self.temp_entries.find({
                "trip_id": trip_id,
                "bus_id": bus_id
            }))
            
            print(f"🔍 Found {len(remaining)} unmatched ENTRY records for {bus_id}")
            
            unmatched_count = 0
            for entry in remaining:
                unmatched_entry = {
                    "trip_id": trip_id,
                    "bus_id": bus_id,
                    "route_name": entry.get('route_name', self.route_name),
                    "type": "ENTRY",  # These are ENTRY faces that never got an EXIT match
                    "trip_start_time": current_trip['start_time'],
                    "face_id": entry.get('face_id', 0),
                    "face_embedding": entry.get('face_embedding', []),
                    "embedding_size": entry.get('embedding_size', 0),
                    "location": entry.get('entry_location', {}),
                    "timestamp": entry.get('entry_timestamp'),
                    "best_similarity_found": 0.0,
                    "reason": "Trip ended - no exit match found",
                    "created_at": datetime.now()
                }
                self.unmatched_passengers.insert_one(unmatched_entry)
                unmatched_count += 1
                print(f"   ➡️ Moved ENTRY face_id={entry.get('face_id')} to unmatchedPassengers")
            
            # Delete temp entries for this trip to prevent carryover to next trip
            deleted_count = self.temp_entries.delete_many({"trip_id": trip_id}).deleted_count
            print(f"🗑️ Deleted {deleted_count} temp_entries for trip {trip_id}")
            
            # Update trip session
            self.trip_sessions.update_one(
                {"_id": current_trip['_id']},
                {
                    "$set": {
                        "status": "completed",
                        "end_time": datetime.utcnow(),
                        "total_passengers": passenger_count,
                        "total_unmatched": unmatched_count
                    }
                }
            )
            
            print(f"✅ Ended trip for {bus_id}: {trip_id}")
            print(f"   Passengers: {passenger_count}, Unmatched: {unmatched_count}")
            
            # Remove from current trips
            del self.current_trips[bus_id]
            return True
        except Exception as e:
            print(f"❌ Error ending trip for {bus_id}: {e}")
            return False
    
    # Removed: get_current_route_info() - Replaced by DynamicScheduleManager
    # Removed: is_departure_time() - Replaced by DynamicScheduleManager
    
    def get_current_trip(self, bus_id=None):
        """Get current trip information for specific bus"""
        if bus_id is None:
            bus_id = self.default_bus_id
        
        current_trip = self.current_trips.get(bus_id)
        if not current_trip:
            return {
                'trip_id': None,
                'bus_id': bus_id,
                'route_name': self.route_name,
                'status': 'waiting_for_departure',
                'trip_active': False,
                'passengers_inside': 0,
                'passengers_completed': 0,
                'duration_minutes': 0
            }
        
        try:
            trip_session = self.trip_sessions.find_one({"_id": current_trip['_id']})
            
            if trip_session:
                passenger_count = self.final_passengers.count_documents({"trip_id": current_trip['trip_id']})
                temp_count = self.temp_entries.count_documents({"trip_id": current_trip['trip_id']})
                duration_minutes = (datetime.now() - trip_session['start_time']).total_seconds() / 60
                
                # Simplified status
                if duration_minutes < 60:
                    trip_status = "departing"
                elif duration_minutes < 480:  # 8 hours
                    trip_status = "in_transit"
                else:
                    trip_status = "approaching_destination"
                
                return {
                    'trip_id': current_trip['trip_id'],
                    'bus_id': bus_id,
                    'route_name': trip_session.get('route_name', self.route_name),
                    'start_time': trip_session['start_time'].isoformat(),
                    'status': trip_status,
                    'trip_active': True,
                    'passengers_completed': passenger_count,
                    'passengers_inside': temp_count,
                    'duration_minutes': round(duration_minutes, 1)
                }
            return None
        except Exception as e:
            print(f"❌ Error getting trip for {bus_id}: {e}")
            return None
    
    def get_all_trips(self, limit=10, bus_id=None):
        """Get recent trips for specific bus or all buses"""
        try:
            query = {}
            if bus_id:
                query["bus_id"] = bus_id
            
            trips = list(self.trip_sessions.find(query).sort("start_time", -1).limit(limit))
            
            return trips
        except Exception as e:
            print(f"❌ Error getting trips: {e}")
            return []
    
    def generate_passenger_id(self):
        """Generate unique passenger ID"""
        count = self.final_passengers.count_documents({}) + 1
        return f"PASS_{count:06d}"
    
    def calculate_haversine_distance(self, lat1, lon1, lat2, lon2):
        """Calculate straight-line distance between two points using Haversine formula"""
        try:
            # Convert latitude and longitude from degrees to radians
            lat1, lon1, lat2, lon2 = map(math.radians, [float(lat1), float(lon1), float(lat2), float(lon2)])
            
            # Haversine formula
            dlat = lat2 - lat1
            dlon = lon2 - lon1
            a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
            c = 2 * math.asin(math.sqrt(a))
            
            # Radius of earth in kilometers
            r = 6371
            
            return c * r
        except Exception as e:
            print(f"❌ Error calculating Haversine distance: {e}")
            return 0.0
    
    def calculate_road_distance_osrm(self, start_lat, start_lon, end_lat, end_lon):
        """Calculate road distance using OSRM API (free, no API key required)"""
        try:
            url = f"{self.distance_api_config['osrm_base_url']}/{start_lon},{start_lat};{end_lon},{end_lat}"
            params = {
                'overview': 'false',
                'geometries': 'geojson',
                'steps': 'false'
            }
            
            response = requests.get(url, params=params, timeout=self.distance_api_config['timeout'])
            
            if response.status_code == 200:
                data = response.json()
                if data.get('code') == 'Ok' and data.get('routes'):
                    # Distance in meters, convert to kilometers
                    distance_km = data['routes'][0]['distance'] / 1000
                    duration_seconds = data['routes'][0]['duration']
                    
                    return {
                        'distance_km': round(distance_km, 2),
                        'duration_minutes': round(duration_seconds / 60, 1),
                        'provider': 'osrm',
                        'success': True
                    }
            
            print(f"❌ OSRM API error: {response.status_code} - {response.text}")
            return None
            
        except Exception as e:
            print(f"❌ Error with OSRM API: {e}")
            return None
    
    # Removed: calculate_road_distance_openrouteservice() - Unused, OSRM is default
    
    def calculate_road_distance(self, start_lat, start_lon, end_lat, end_lon):
        """Calculate road distance using OSRM with Haversine fallback"""
        try:
            if not all([start_lat, start_lon, end_lat, end_lon]):
                return None
            
            start_lat, start_lon = float(start_lat), float(start_lon)
            end_lat, end_lon = float(end_lat), float(end_lon)
            
            if not (-90 <= start_lat <= 90 and -180 <= start_lon <= 180 and 
                    -90 <= end_lat <= 90 and -180 <= end_lon <= 180):
                return None
            
            # Try OSRM first
            result = self.calculate_road_distance_osrm(start_lat, start_lon, end_lat, end_lon)
            
            # Fallback to Haversine if OSRM fails
            if not result:
                haversine_km = self.calculate_haversine_distance(start_lat, start_lon, end_lat, end_lon)
                result = {
                    'distance_km': round(haversine_km, 2),
                    'duration_minutes': round(haversine_km * 2, 1),
                    'provider': 'haversine_fallback',
                    'success': True,
                    'note': 'Straight-line distance'
                }
            
            return result
            
        except Exception as e:
            print(f"❌ Error calculating road distance: {e}")
            return None
    
    def reverse_geocode(self, lat, lon):
        """
        Convert coordinates to location name using Nominatim API
        Cache results to avoid repeated API calls
        """
        try:
            # Round coordinates to 4 decimal places for caching (~11m precision)
            cache_key = f"{round(lat, 4)}_{round(lon, 4)}"
            
            # Check cache first
            if not hasattr(self, '_location_cache'):
                self._location_cache = {}
            
            if cache_key in self._location_cache:
                return self._location_cache[cache_key]
            
            # Call Nominatim API
            url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=14&addressdetails=1"
            headers = {'User-Agent': 'BusPassengerTracker/1.0'}
            
            response = requests.get(url, headers=headers, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                address = data.get('address', {})
                
                # Extract meaningful location name
                location_name = (
                    address.get('city') or 
                    address.get('town') or 
                    address.get('village') or 
                    address.get('suburb') or 
                    address.get('county') or 
                    address.get('state') or 
                    'Unknown Location'
                )
                
                # Cache the result
                self._location_cache[cache_key] = location_name
                
                # Rate limiting - wait 1 second between API calls
                time.sleep(1)
                
                return location_name
            else:
                print(f"⚠️ Geocoding failed: HTTP {response.status_code}")
                return None
                
        except Exception as e:
            print(f"⚠️ Reverse geocoding error: {e}")
            return None
    
    def calculate_fare(self, distance_km):
        """Calculate fare based on distance using fareStages collection from MongoDB"""
        try:
            if not distance_km or distance_km <= 0:
                return 0.0
            
            # If distance is less than 100 meters (0.1 km), set price to 0
            if distance_km < 0.1:
                print(f"⚠️ Distance too short ({distance_km} km < 100m), setting price to 0")
                return 0.0
            
            # Calculate stage number (2 km per stage - official Sri Lankan bus fare system)
            STAGE_DISTANCE = 2.0
            stage_number = math.ceil(distance_km / STAGE_DISTANCE)
            
            # Fetch fare from fareStages collection
            fare_stage = self.db['fareStages'].find_one({
                'stage_number': stage_number,
                'is_active': True
            })
            
            if fare_stage:
                return float(fare_stage['fare'])
            
            # If exact stage not found, find the closest higher stage
            closest_stage = self.db['fareStages'].find_one({
                'stage_number': {'$gte': stage_number},
                'is_active': True
            }, sort=[('stage_number', 1)])
            
            if closest_stage:
                return float(closest_stage['fare'])
            
            # Fallback: use highest available stage
            highest_stage = self.db['fareStages'].find_one(
                {'is_active': True},
                sort=[('stage_number', -1)]
            )
            
            if highest_stage:
                return float(highest_stage['fare'])
            
            # Final fallback: use old hardcoded calculation
            print(f"⚠️ No fare stages found in database, using fallback calculation")
            if stage_number == 1:
                return 30.0
            else:
                return 30.0 + ((stage_number - 1) * 10.0)
            
        except Exception as e:
            print(f"❌ Error calculating fare: {e}")
            return 0.0
    
    def check_season_ticket_member(self, face_embedding, bus_route=None, gps_location=None):
        """
        OPTIMIZED: Check if face matches season ticket members for THIS ROUTE ONLY
        
        Args:
            face_embedding: Face embedding to match
            bus_route: Current bus route (optional, for optimization)
            gps_location: Current GPS location (optional, for route detection)
        
        Returns:
            tuple: (member, similarity) or (None, 0.0)
        """
        try:
            if not face_embedding or len(face_embedding) == 0:
                print("⚠️ No face embedding provided for season ticket check")
                return None, 0.0
            
            now = datetime.now()
            
            # 2. Build query for active members
            query = {
                "is_active": True,
                "valid_from": {"$lte": now},
                "valid_until": {"$gte": now},
                "face_embedding": {"$exists": True, "$ne": []}
            }
            
            # 3. OPTIMIZATION: Filter members by route if route_name is provided
            # This implements the user's request: "only check if bus has this waypoint"
            if bus_route:
                # We assume bus_route passed here is the route_name (e.g., "Jaffna-Colombo")
                print(f"🛤️ Filtering season tickets for route: {bus_route}")
                # Check if member's valid_routes patterns match this bus route
                # or if the bus route name contains member's from/to locations
                # We'll fetch all and filter in Python for complex pattern logic
                all_active_members = list(self.season_ticket_members.find(query))
                
                filtered_members = []
                for m in all_active_members:
                    is_relevant = False
                    for vr in m.get('valid_routes', []):
                        patterns = vr.get('route_patterns', [])
                        from_l = vr.get('from_location', '').lower()
                        to_l = vr.get('to_location', '').lower()
                        
                        # Case 1: Direct pattern match
                        if any(p.lower() in bus_route.lower() for p in patterns):
                            is_relevant = True
                            break
                        
                        # Case 2: Bus route name mentions the locations
                        # (e.g. Jaffna-Colombo bus serves Jaffna-Kodikamam)
                        if from_l and to_l and from_l in bus_route.lower() and to_l in bus_route.lower():
                            is_relevant = True
                            break
                        
                        # Case 3: No patterns defined, butlocations match
                        if not patterns and (from_l in bus_route.lower() or to_l in bus_route.lower()):
                            is_relevant = True
                            break
                    
                    if is_relevant:
                        filtered_members.append(m)
                
                all_active_members = filtered_members
                print(f"📊 Filtered to {len(all_active_members)} relevant season ticket members for this route")
            else:
                all_active_members = list(self.season_ticket_members.find(query))
            
            if not all_active_members:
                print("⚠️ No active season ticket members found in database")
                return None, 0.0
            
            print(f"🔍 Checking against {len(all_active_members)} active season ticket members")
            
            # Convert input embedding to numpy array
            input_array = np.array(face_embedding, dtype=np.float32).reshape(1, -1)
            print(f"📊 Input embedding size: {input_array.shape}")
            
            best_match = None
            best_similarity = 0.0
            all_similarities = []
            
            for member in all_active_members:
                if not member.get('face_embedding'):
                    continue
                
                member_name = member.get('name', 'Unknown')
                member_id = member.get('member_id', 'Unknown')
                
                # Convert member embedding to numpy array
                member_array = np.array(member['face_embedding'], dtype=np.float32).reshape(1, -1)
                
                # Calculate cosine similarity
                similarity = cosine_similarity(input_array, member_array)[0][0]
                all_similarities.append((member_name, member_id, similarity))
                
                print(f"   �n {member_name} ({member_id}): similarity = {similarity:.4f} (threshold: {self.season_ticket_similarity_threshold})")
                
                if similarity > best_similarity:
                    best_similarity = similarity
                    if similarity > self.season_ticket_similarity_threshold:
                        best_match = member
            
            # Print summary
            print(f"\n📊 Season Ticket Check Summary:")
            print(f"   Best similarity: {best_similarity:.4f}")
            print(f"   Threshold: {self.season_ticket_similarity_threshold}")
            print(f"   Match found: {'YES ✅' if best_match else 'NO ❌'}")
            
            if best_match:
                print(f"🎫 Season ticket member detected: {best_match['name']} (similarity: {best_similarity:.3f})")
                return best_match, best_similarity
            else:
                if best_similarity > 0:
                    print(f"⚠️ Closest match was {best_similarity:.4f}, below threshold {self.season_ticket_similarity_threshold}")
                    print(f"💡 TIP: Consider lowering season_ticket_similarity_threshold if this is a valid member")
            
            return None, 0.0
            
        except Exception as e:
            print(f"❌ Error checking season ticket: {e}")
            import traceback
            traceback.print_exc()
            return None, 0.0

    def check_contractor_match(self, face_embedding, bus_id):
        """
        Check if face matches the contractor for THIS BUS
        
        Args:
            face_embedding: Face embedding to match
            bus_id: Current bus ID
        
        Returns:
            tuple: (contractor, similarity) or (None, 0.0)
        """
        try:
            if not face_embedding or len(face_embedding) == 0:
                return None, 0.0
            
            # Find contractor for this specific bus
            contractor = self.contractors.find_one({"bus_id": bus_id})
            
            if not contractor or not contractor.get('face_embedding'):
                return None, 0.0
            
            print(f"🔍 Checking against contractor for bus {bus_id}: {contractor.get('name')}")
            
            # Convert embeddings to numpy arrays
            input_array = np.array(face_embedding, dtype=np.float32).reshape(1, -1)
            contractor_array = np.array(contractor['face_embedding'], dtype=np.float32).reshape(1, -1)
            
            # Calculate cosine similarity
            similarity = cosine_similarity(input_array, contractor_array)[0][0]
            
            print(f"   👤 Contractor Similarity: {similarity:.4f} (threshold: {self.contractor_similarity_threshold})")
            
            if similarity > self.contractor_similarity_threshold:
                print(f"✅ Contractor Match Found: {contractor['name']}")
                return contractor, similarity
            
            return None, similarity
            
        except Exception as e:
            print(f"❌ Error checking contractor match: {e}")
            return None, 0.0
    
    # Removed: _get_nearby_stops() - Never called
    # Removed: _get_location_name_variations() - Never called
    # Removed: _location_matches() - Never called
    
    def is_route_valid_for_season_ticket(self, member, entry_location, exit_location):
        """Check if journey is within season ticket valid routes using GPS-based detection"""
        try:
            if not member.get('valid_routes') or len(member['valid_routes']) == 0:
                # No route restrictions - valid everywhere
                print("✅ No route restrictions - valid everywhere")
                return True, None
            
            entry_lat = entry_location.get('latitude')
            entry_lon = entry_location.get('longitude')
            exit_lat = exit_location.get('latitude')
            exit_lon = exit_location.get('longitude')
            
            # Validate GPS coordinates
            if not all([entry_lat, entry_lon, exit_lat, exit_lon]):
                print("⚠️ Missing GPS coordinates, falling back to route name matching")
                # Fallback to old method if GPS not available
                return self._fallback_route_validation(member)
            
            # Use route detector if available
            if self.route_detector:
                print(f"🗺️ Using GPS-based route detection")
                print(f"   Entry: {entry_lat}, {entry_lon}")
                print(f"   Exit: {exit_lat}, {exit_lon}")
                
                is_valid, match_info = self.route_detector.find_matching_season_ticket_routes(
                    entry_lat, entry_lon,
                    exit_lat, exit_lon,
                    member['valid_routes']
                )
                
                if is_valid:
                    print(f"✅ GPS-based validation: Journey matches {match_info.get('matched_route')}")
                    return True, match_info
                else:
                    print(f"❌ GPS-based validation: {match_info.get('reason')}")
                    return False, match_info
            else:
                # Fallback if route detector not available
                print("⚠️ Route detector not available, using fallback")
                return self._fallback_route_validation(member)
            
        except Exception as e:
            print(f"❌ Error checking route validity: {e}")
            import traceback
            traceback.print_exc()
            return False, None
    
    def _fallback_route_validation(self, member):
        """Fallback route validation using route name matching"""
        try:
            for valid_route in member['valid_routes']:
                route_patterns = valid_route.get('route_patterns', [])
                
                if not route_patterns or len(route_patterns) == 0:
                    from_loc = valid_route.get('from_location', '').lower()
                    to_loc = valid_route.get('to_location', '').lower()
                    
                    if from_loc in self.route_name.lower() and to_loc in self.route_name.lower():
                        print(f"✅ Fallback: Route {self.route_name} matches {from_loc} → {to_loc}")
                        return True, valid_route
                else:
                    for pattern in route_patterns:
                        if pattern.lower() in self.route_name.lower() or self.route_name.lower() in pattern.lower():
                            print(f"✅ Fallback: Route {self.route_name} matches pattern {pattern}")
                            return True, valid_route
            
            print(f"❌ Fallback: Route {self.route_name} not in member's valid routes")
            return False, None
            
        except Exception as e:
            print(f"❌ Error in fallback validation: {e}")
            return False, None
    
    def store_entry(self, log_entry):
        """Store temporary entry for matching - supports multiple buses"""
        try:
            # Get bus_id from log entry, fallback to default
            bus_id = log_entry.get('bus_id', self.default_bus_id)
            
            # Ensure we have an active trip for this bus
            current_trip = self.get_current_trip_for_bus(bus_id)
            if not current_trip or current_trip.get('status') != 'active':
                print(f"⚠️ No active trip for {bus_id}, starting new trip...")
                self.start_new_trip(bus_id=bus_id)
                current_trip = self.current_trips.get(bus_id)
            
            # Check if this is a season ticket member at entry
            # OPTIMIZED: Pass route and GPS for targeted checking
            gps_location = {
                'latitude': log_entry.get('latitude', 0),
                'longitude': log_entry.get('longitude', 0)
            }
            season_member, season_similarity = self.check_season_ticket_member(
                log_entry.get('face_embedding', []),
                bus_route=current_trip.get('trip_id'),  # Use trip_id as route identifier
                gps_location=gps_location
            )
            
            season_ticket_detected = None
            if season_member:
                season_ticket_detected = {
                    "member_id": season_member['member_id'],
                    "member_name": season_member['name'],
                    "similarity_score": float(season_similarity)
                }
                print(f"🎫 Season ticket member detected at ENTRY on {bus_id}: {season_member['name']}")
            
            temp_entry = {
                "trip_id": current_trip['trip_id'],
                "trip_start_time": current_trip['start_time'],
                "bus_id": bus_id,  # Use bus_id from request
                "route_name": self.route_name,
                "face_id": log_entry.get('face_id', 0),
                "face_embedding": log_entry.get('face_embedding', []),
                "embedding_size": log_entry.get('embedding_size', 0),
                "season_ticket_detected": season_ticket_detected,
                "entry_location": {
                    "latitude": log_entry.get('latitude', 0),
                    "longitude": log_entry.get('longitude', 0),
                    "device_id": log_entry.get('device_id'),
                    "timestamp": log_entry.get('timestamp')
                },
                "entry_timestamp": self._parse_timestamp_safe(log_entry.get('timestamp')),
                "created_at": datetime.utcnow()
            }
            
            result = self.temp_entries.insert_one(temp_entry)
            print(f"✅ Stored entry for {bus_id}: {result.inserted_id} (Trip: {current_trip['trip_id']})")
            return str(result.inserted_id)
            
        except Exception as e:
            print(f"❌ Error storing entry: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def find_matching_entry(self, exit_log):
        """Find matching entry and create final passenger record - supports multiple buses"""
        if not exit_log.get('face_embedding'):
            return None, 0.0
        
        try:
            # Get bus_id from exit log, fallback to default
            bus_id = exit_log.get('bus_id', self.default_bus_id)
            
            # Ensure we have an active trip for this bus
            current_trip = self.get_current_trip_for_bus(bus_id)
            if not current_trip or current_trip.get('status') != 'active':
                print(f"⚠️ No active trip for {bus_id} for exit matching")
                return None, 0.0
            
            # Calculate time threshold for matching window
            time_threshold = datetime.now() - timedelta(hours=self.time_window_hours)
            
            # Build query - MUST filter by bus_id to only match entries from this bus
            query = {
                "trip_id": current_trip['trip_id'],  # Only match within same trip
                "bus_id": bus_id,  # CRITICAL: Only match entries from this bus!
                "entry_timestamp": {"$gte": time_threshold},
                "face_embedding": {"$exists": True, "$ne": []}
            }
            
            # DEBUG: Print query to verify bus_id filtering
            print(f"🔎 Query for matching: bus_id={bus_id}, trip_id={current_trip['trip_id']}")
            
            unmatched_entries = self.temp_entries.find(query).sort("entry_timestamp", -1)
            
            entries_list = list(unmatched_entries)
            print(f"🔍 Found {len(entries_list)} entries for {bus_id} (Trip: {current_trip['trip_id']})")
            
            # DEBUG: Show which bus_ids are in the results
            if entries_list:
                found_bus_ids = set(e.get('bus_id', 'UNKNOWN') for e in entries_list)
                print(f"   Bus IDs in results: {found_bus_ids}")
            
            if not entries_list:
                print(f"❌ No unmatched entries found for bus {bus_id}")
                print(f"   Time threshold: {time_threshold}")
                return None, 0.0
            
            print(f"🔍 Checking {len(entries_list)} entries for similarity")
            
            # Convert exit embedding to numpy array
            exit_array = np.array(exit_log['face_embedding'], dtype=np.float32).reshape(1, -1)
            
            best_match = None
            best_similarity = 0.0
            
            for entry in entries_list:
                if not entry.get('face_embedding'):
                    continue
                
                # Convert entry embedding to numpy array
                entry_array = np.array(entry['face_embedding'], dtype=np.float32).reshape(1, -1)
                
                # Calculate cosine similarity
                similarity = cosine_similarity(exit_array, entry_array)[0][0]
                
                print(f"  Entry {entry['_id']}: similarity = {similarity:.3f} (threshold: {self.similarity_threshold})")
                
                if similarity > best_similarity and similarity > self.similarity_threshold:
                    best_similarity = similarity
                    best_match = entry
            
            print(f"🎯 Best match: similarity = {best_similarity:.3f}")
            
            if best_match:
                # Create final passenger record
                passenger_id = self.generate_passenger_id()
                
                # Calculate road distance between entry and exit points
                distance_info = self.calculate_road_distance(
                    best_match['entry_location']['latitude'],
                    best_match['entry_location']['longitude'],
                    exit_log.get('latitude', 0),
                    exit_log.get('longitude', 0)
                )
                
                # Check if passenger is a season ticket member
                # OPTIMIZED: Pass route and GPS for targeted checking
                exit_gps_location = {
                    'latitude': exit_log.get('latitude', 0),
                    'longitude': exit_log.get('longitude', 0)
                }
                season_member, season_similarity = self.check_season_ticket_member(
                    exit_log['face_embedding'],
                    bus_route=current_trip.get('route_name'), # Pass route_name for filtering
                    gps_location=exit_gps_location
                )
                
                is_season_ticket = False
                season_ticket_info = None
                price = 0.0
                
                if season_member:
                    # Check if route is valid for this season ticket
                    exit_location = {
                        'latitude': exit_log.get('latitude', 0),
                        'longitude': exit_log.get('longitude', 0)
                    }
                    is_route_valid, valid_route = self.is_route_valid_for_season_ticket(
                        season_member, 
                        best_match['entry_location'], 
                        exit_location
                    )
                    
                    if is_route_valid:
                        # Season ticket is valid for this route - no charge
                        is_season_ticket = True
                        price = 0.0
                        season_ticket_info = {
                            "member_id": season_member['member_id'],
                            "member_name": season_member['name'],
                            "ticket_type": season_member.get('ticket_type', 'monthly'),
                            "valid_until": season_member['valid_until'].isoformat() if season_member.get('valid_until') else None,
                            "similarity_score": float(season_similarity),
                            "valid_route": valid_route
                        }
                        print(f"🎫 Season ticket applied: {season_member['name']} - Price: ₹0")
                        
                        # Update member statistics
                        self.season_ticket_members.update_one(
                            {"_id": season_member['_id']},
                            {
                                "$inc": {"total_trips": 1},
                                "$set": {"last_used": datetime.now()}
                            }
                        )
                    else:
                        # Season ticket not valid for this route - calculate normal price
                        distance_km = distance_info.get('distance_km', 0) if distance_info else 0
                        price = self.calculate_fare(distance_km)
                        print(f"⚠️ Season ticket not valid for route {self.route_name} - Charging normal price: ₹{price}")
                else:
                    # Regular passenger - calculate normal price
                    distance_km = distance_info.get('distance_km', 0) if distance_info else 0
                    price = self.calculate_fare(distance_km)
                
                stage_number = math.ceil(distance_info.get('distance_km', 0) / 2.0) if distance_info and distance_info.get('distance_km', 0) > 0 else 0
                
                # Reverse geocode locations to get place names
                entry_location_name = self.reverse_geocode(
                    best_match['entry_location']['latitude'],
                    best_match['entry_location']['longitude']
                )
                exit_location_name = self.reverse_geocode(
                    exit_log.get('latitude', 0),
                    exit_log.get('longitude', 0)
                )
                
                final_passenger = {
                    "id": passenger_id,
                    "trip_id": current_trip['trip_id'],
                    "trip_start_time": current_trip['start_time'],
                    "bus_id": bus_id,  # Use bus_id from request
                    "route_name": self.route_name,
                    "is_season_ticket": is_season_ticket,
                    "season_ticket_info": season_ticket_info,
                    "entryLocation": {
                        "latitude": best_match['entry_location']['latitude'],
                        "longitude": best_match['entry_location']['longitude'],
                        "device_id": best_match['entry_location']['device_id'],
                        "timestamp": best_match['entry_location']['timestamp'],
                        "location_name": entry_location_name  # NEW!
                    },
                    "exitLocation": {
                        "latitude": exit_log.get('latitude', 0),
                        "longitude": exit_log.get('longitude', 0),
                        "device_id": exit_log.get('device_id'),
                        "timestamp": exit_log.get('timestamp'),
                        "location_name": exit_location_name  # NEW!
                    },
                    "entry_timestamp": best_match['entry_timestamp'],
                    "exit_timestamp": self._parse_timestamp_safe(exit_log.get('timestamp')),
                    "journey_duration_minutes": (self._parse_timestamp_safe(exit_log.get('timestamp')) - best_match['entry_timestamp']).total_seconds() / 60,
                    "similarity_score": float(best_similarity),
                    "entry_face_id": best_match.get('face_id', 0),
                    "exit_face_id": exit_log.get('face_id', 0),
                    "distance_info": distance_info if distance_info else {
                        "distance_km": 0.0,
                        "duration_minutes": 0.0,
                        "provider": "unavailable",
                        "success": False,
                        "note": "Distance calculation failed"
                    },
                    "price": price,
                    "stage_number": stage_number,
                    "created_at": datetime.now()
                }
                
                # Insert final passenger record
                result = self.final_passengers.insert_one(final_passenger)
                
                # Delete the temporary entry immediately
                self.temp_entries.delete_one({"_id": best_match['_id']})
                
                print(f"✅ Created final passenger: {passenger_id}")
                if distance_info and distance_info.get('success'):
                    print(f"📏 Journey distance: {distance_info['distance_km']} km (estimated {distance_info['duration_minutes']} min)")
                print(f"🗑️ Deleted temporary entry: {best_match['_id']}")
                
                return final_passenger, best_similarity
            
            return None, best_similarity
            
        except Exception as e:
            print(f"❌ Error finding matching entry: {e}")
            return None, 0.0
    
    def process_face_log(self, log_entry):
        """Process incoming face log entry - supports multiple buses"""
        location_type = log_entry.get('location_type', '').upper()
        bus_id = log_entry.get('bus_id', self.default_bus_id)
        
        # 1. Prefer extraction from IMAGE if image_data is provided
        # This ensures the embedding model matches what was used for Contractor registration
        image_data = log_entry.get('image_data') or log_entry.get('image')
        if image_data:
            print(f"🖼️ Found image_data in log. Extracting fresh embedding for consistency...")
            try:
                from face_recognition_helper import extract_face_embedding_from_base64
                extract_res = extract_face_embedding_from_base64(image_data, draw_boxes=False)
                if extract_res.get('success') and extract_res.get('face_embedding'):
                    log_entry['face_embedding'] = extract_res['face_embedding']
                    print(f"✅ Successfully extracted fresh embedding from image ({extract_res.get('is_mock', False) and 'MOCK' or 'REAL'})")
            except Exception as e:
                print(f"⚠️ Failed to extract embedding from image_data: {e}")

        face_embedding = log_entry.get('face_embedding', [])

        # FIRST: Check if this is the CONTRACTOR for this bus
        contractor, contractor_sim = self.check_contractor_match(face_embedding, bus_id)
        if contractor:
            print(f"🛡️ CONTRACTOR DETECTED: {contractor['name']} at {location_type} on {bus_id}. Skipping processing.")
            return {
                'action': 'ignored_contractor',
                'contractor_name': contractor['name'],
                'bus_id': bus_id,
                'face_id': log_entry.get('face_id', 0),
                'similarity': float(contractor_sim),
                'message': f'🛡️ Contractor {contractor["name"]} matched. Ignored.'
            }
        
        if location_type == 'ENTRY':
            # Store temporary entry
            entry_id = self.store_entry(log_entry)
            
            if entry_id:
                return {
                    'action': 'stored_entry',
                    'entry_id': entry_id,
                    'bus_id': bus_id,  # Use bus_id from request
                    'face_id': log_entry.get('face_id', 0),
                    'message': f'Entry stored for {bus_id} (face_id: {log_entry.get("face_id", 0)})'
                }
            else:
                return {
                    'action': 'error',
                    'bus_id': bus_id,
                    'message': 'Failed to store entry'
                }
        
        elif location_type == 'EXIT':
            # Find matching entry and create final record
            match_result, similarity = self.find_matching_entry(log_entry)
            
            if match_result:
                return {
                    'action': 'matched_journey',
                    'passenger_id': match_result['id'],
                    'bus_id': bus_id,  # Use bus_id from request
                    'entry_face_id': match_result['entry_face_id'],
                    'exit_face_id': match_result['exit_face_id'],
                    'similarity': float(similarity),
                    'journey_duration': match_result['journey_duration_minutes'],
                    'message': f'✅ Journey on {bus_id}! Passenger {match_result["id"]} (similarity: {similarity:.3f}, duration: {match_result["journey_duration_minutes"]:.1f} min)'
                }
            else:
                # Store unmatched exit
                unmatched_exit_id = self.store_unmatched_exit(log_entry, similarity)
                return {
                    'action': 'unmatched_exit',
                    'bus_id': bus_id,  # Use bus_id from request
                    'face_id': log_entry.get('face_id', 0),
                    'best_similarity': float(similarity),
                    'unmatched_id': unmatched_exit_id,
                    'message': f'❌ No match on {bus_id} for exit face_id {log_entry.get("face_id", 0)} (best: {similarity:.3f})'
                }
        
        else:
            return {
                'action': 'error',
                'bus_id': bus_id,
                'message': f'Unknown location_type: {location_type}'
            }
    
    def store_unmatched_exit(self, exit_log, best_similarity):
        """Store unmatched exit passenger - supports multiple buses"""
        try:
            # Get bus_id from exit log
            bus_id = exit_log.get('bus_id', self.default_bus_id)
            
            # Get current trip for this bus
            current_trip = self.get_current_trip_for_bus(bus_id)
            
            # Determine trip details (with fallback)
            if current_trip and current_trip.get('status') == 'active':
                trip_id = current_trip['trip_id']
                trip_start_time = current_trip['start_time']
            else:
                print(f"⚠️ No active trip for {bus_id} - storing unmatched exit with FALLBACK trip")
                trip_id = f"FALLBACK_{bus_id}_{datetime.now().strftime('%Y%m%d_%H%M')}"
                trip_start_time = datetime.utcnow()
            
            unmatched_exit = {
                "trip_id": trip_id,
                "trip_start_time": trip_start_time,
                "bus_id": bus_id,  # Use bus_id from request
                "route_name": self.route_name,
                "type": "EXIT",
                "face_id": exit_log.get('face_id', 0),
                "face_embedding": exit_log.get('face_embedding', []),
                "embedding_size": exit_log.get('embedding_size', 0),
                "location": {
                    "latitude": exit_log.get('latitude', 0),
                    "longitude": exit_log.get('longitude', 0),
                    "device_id": exit_log.get('device_id'),
                    "timestamp": exit_log.get('timestamp')
                },
                "timestamp": self._parse_timestamp_safe(exit_log.get('timestamp')),
                "best_similarity_found": float(best_similarity),
                "reason": "No matching entry found",
                "created_at": datetime.utcnow()
            }
            
            result = self.unmatched_passengers.insert_one(unmatched_exit)
            print(f"📝 Stored unmatched exit for {bus_id}: {result.inserted_id}")
            return str(result.inserted_id)
            
        except Exception as e:
            print(f"❌ Error storing unmatched exit: {e}")
            return None
    
    def cleanup_old_temp_entries(self, hours_old=24, bus_id=None, trip_id=None):
        """Move old/orphaned temp entries to unmatched collection - supports multiple buses
        
        Args:
            hours_old: Clean entries older than this many hours. If 0, clean ALL entries for the specified bus/trip
            bus_id: Filter cleanup to specific bus
            trip_id: Filter cleanup to specific trip
        """
        try:
            # Build query based on parameters
            query = {}
            
            if hours_old > 0:
                # Time-based cleanup - clean entries older than hours_old
                cutoff_time = datetime.now() - timedelta(hours=hours_old)
                query["entry_timestamp"] = {"$lt": cutoff_time}
                reason = f"No exit found within {hours_old} hours"
            else:
                # Trip end cleanup - clean ALL remaining entries for this bus/trip
                reason = "Trip ended - no exit match found"
            
            # Add bus_id filter if specified
            if bus_id:
                query["bus_id"] = bus_id
            
            # Add trip_id filter if specified  
            if trip_id:
                query["trip_id"] = trip_id
            
            # Find entries to clean up
            entries_to_clean = list(self.temp_entries.find(query))
            
            if entries_to_clean:
                print(f"🔍 Found {len(entries_to_clean)} temp entries to clean" + (f" for {bus_id}" if bus_id else ""))
                
                for entry in entries_to_clean:
                    entry_bus_id = entry.get('bus_id', self.default_bus_id)
                    unmatched_entry = {
                        "trip_id": entry.get('trip_id', 'UNKNOWN'),
                        "bus_id": entry_bus_id,
                        "route_name": entry.get('route_name', self.route_name),
                        "type": "ENTRY",
                        "trip_start_time": entry.get('trip_start_time'),
                        "face_id": entry.get('face_id', 0),
                        "face_embedding": entry.get('face_embedding', []),
                        "embedding_size": entry.get('embedding_size', 0),
                        "location": entry.get('entry_location', {}),
                        "timestamp": entry.get('entry_timestamp'),
                        "best_similarity_found": 0.0,
                        "reason": reason,
                        "created_at": datetime.now()
                    }
                    self.unmatched_passengers.insert_one(unmatched_entry)
                    print(f"   ➡️ Moved ENTRY face_id={entry.get('face_id')} to unmatchedPassengers")
                
                result = self.temp_entries.delete_many(query)
                
                print(f"🗑️ Cleaned up {result.deleted_count} temp entries" + (f" for {bus_id}" if bus_id else ""))
                return len(entries_to_clean)
            else:
                print(f"✅ No temp entries to clean" + (f" for {bus_id}" if bus_id else ""))
            
            return 0
            
        except Exception as e:
            print(f"❌ Error during cleanup: {e}")
            return 0

    def is_within_trip_schedule(self, current_time_str, trip_start, trip_end):
        """Check if current time is within ESP32 trip schedule (handles overnight trips)"""
        try:
            from datetime import datetime
            
            # Parse times
            current = datetime.strptime(current_time_str, "%H:%M").time()
            start = datetime.strptime(trip_start, "%H:%M").time()
            end = datetime.strptime(trip_end, "%H:%M").time()
            
            if start <= end:
                # Same day trip (e.g., 08:00 - 18:00)
                return start <= current <= end
            else:
                # Overnight trip (e.g., 20:30 - 08:30 next day)
                return current >= start or current <= end
                
        except Exception as e:
            print(f"❌ Error parsing trip schedule: {e}")
            return False
    
    # Removed: get_stats() - Never called, stats handled by Node.js backend

# Global tracker instance
bus_tracker = SimplifiedBusTracker()

# Add dynamic schedule manager (replaces old trip_scheduler)
schedule_manager = DynamicScheduleManager()
schedule_manager.start_scheduler_thread()

print("✅ Using DynamicScheduleManager for automated trip scheduling")

# Power Management Functions
def get_power_config(bus_id):
    """Get power configuration for a bus"""
    try:
        config = bus_tracker.power_configs.find_one({"bus_id": bus_id})
        
        if not config:
            # Create default config
            default_config = {
                "bus_id": bus_id,
                "bus_name": f"Bus {bus_id}",
                "deep_sleep_enabled": True,
                "maintenance_interval": 5,
                "maintenance_duration": 3,
                "last_updated": datetime.now(),
                "boards": []
            }
            bus_tracker.power_configs.insert_one(default_config)
            config = default_config
        
        # Remove MongoDB _id
        if '_id' in config:
            del config['_id']
            
        # ENRICH WITH DYNAMIC SCHEDULE (LIVE FETCH)
        if schedule_manager:
            try:
                # Always fetch the latest schedule for THIS specific bus from MongoDB
                trip_windows = schedule_manager.get_todays_trip_windows(bus_id=bus_id)
                if trip_windows:
                    print(f"✅ Injecting {len(trip_windows)} LIVE trip windows for {bus_id}")
                    config['trip_windows'] = trip_windows
                    config['use_multi_trip'] = True
                else:
                    print(f"ℹ️ No active schedule windows found in MongoDB for {bus_id}")
            except Exception as e:
                print(f"⚠️ Error injecting dynamic schedule for {bus_id}: {e}")
        
        # Inject current server time for ESP32 sync
        config['current_server_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        return config
    except Exception as e:
        print(f"❌ Error getting power config: {e}")
        return None

def update_power_config(bus_id, config_data):
    """Update power configuration for a bus"""
    try:
        update_data = {
            "bus_id": bus_id,
            "bus_name": config_data.get('bus_name', f"Bus {bus_id}"),
            "deep_sleep_enabled": config_data.get('deep_sleep_enabled', True),
            "trip_start": config_data.get('trip_start', '00:00'),
            "trip_end": config_data.get('trip_end', '23:59'),
            "maintenance_interval": config_data.get('maintenance_interval', 5),
            "maintenance_duration": config_data.get('maintenance_duration', 3),
            "last_updated": datetime.now()
        }
        
        # Upsert (update or insert)
        bus_tracker.power_configs.update_one(
            {"bus_id": bus_id},
            {"$set": update_data},
            upsert=True
        )
        
        print(f"✅ Power config updated for {bus_id}")
        return True
    except Exception as e:
        print(f"❌ Error updating power config: {e}")
        return False

# Removed: update_board_heartbeat() - Not used by ESP32 hardware

# Removed: delete_power_config() - Not exposed via API, unused

class SimplifiedHandler(BaseHTTPRequestHandler):
    def _send_json_response(self, data, status_code=200):
        """Helper method to send JSON response with CORS headers"""
        response_data = json.dumps(data, default=str).encode()
        self.send_response(status_code)
        self.send_header('Content-type', 'application/json')
        self.send_header('Content-Length', str(len(response_data)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(response_data)
    
    def _send_error_response(self, message, status_code=500):
        """Helper method to send error response"""
        self._send_json_response({'status': 'error', 'message': str(message)}, status_code)
    
    def _get_query_params(self):
        """Helper method to parse query parameters"""
        from urllib.parse import parse_qs, urlparse
        parsed_url = urlparse(self.path)
        return parse_qs(parsed_url.query)
    
    def do_OPTIONS(self):
        """Handle OPTIONS requests for CORS"""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    
    def do_GET(self):
        """Handle GET requests - ESP32 ENDPOINTS ONLY"""
        # Root status endpoint
        if self.path == '/' or self.path == '/status':
            response = {
                'status': 'success',
                'message': '🚌 Python Backend - ESP32 Processing Engine Only',
                'bus_id': bus_tracker.bus_id,
                'route_name': bus_tracker.route_name,
                'esp32_endpoints': {
                    'GET': [
                        '/api/health',
                        '/api/trip-context',
                        '/api/power-config'
                    ],
                    'POST': [
                        '/api/entry-logs',
                        '/api/exit-logs',
                        '/api/extract-face-embedding'
                    ]
                },
                'note': 'All frontend CRUD endpoints moved to Node.js backend (port 5000)',
                'timestamp': datetime.now().isoformat()
            }
            self._send_json_response(response)
        
        # ESP32 Health Check Endpoint
        elif self.path == '/api/health':
            response = {
                "status": "healthy",
                "timestamp": datetime.now().isoformat(),
                "bus_id": bus_tracker.bus_id
            }
            self._send_json_response(response)
        
        # ESP32 Trip Context Endpoint - OPTIMIZED
        elif self.path.startswith('/api/trip-context'):
            try:
                query_params = self._get_query_params()
                bus_id = query_params.get('bus_id', [bus_tracker.bus_id])[0]
                esp32_trip_start = query_params.get('trip_start', [None])[0]
                esp32_trip_end = query_params.get('trip_end', [None])[0]
                
                # Get current time
                current_time = datetime.now()
                current_time_str = current_time.strftime("%H:%M")
                
                # AUTOMATIC TRIP MANAGEMENT based on ESP32 schedule
                if esp32_trip_start and esp32_trip_end:
                    print(f"ESP32 provided schedule: {esp32_trip_start} - {esp32_trip_end}", flush=True)
                    
                    try:
                        # Check if current time is within ESP32 trip schedule
                        is_esp32_trip_time = bus_tracker.is_within_trip_schedule(current_time_str, esp32_trip_start, esp32_trip_end)
                        
                        # Get current trip for THIS specific bus
                        current_trip = bus_tracker.get_current_trip(bus_id=bus_id)
                        
                        if is_esp32_trip_time:
                            # Within ESP32 trip time - ensure we have an active trip
                            if not current_trip or current_trip.get('status') != 'active':
                                print(f"AUTO-START: Creating trip for ESP32 schedule {esp32_trip_start}-{esp32_trip_end} on {bus_id}", flush=True)
                                # Move any old temp_entries to unmatched before starting new trip
                                # Use hours_old=0 to clean ALL remaining entries for this bus
                                print(f"cleaning old entries for {bus_id}...", flush=True)
                                bus_tracker.cleanup_old_temp_entries(hours_old=0, bus_id=bus_id)
                                # Auto-start trip for this specific bus
                                print(f"starting new trip for {bus_id}...", flush=True)
                                bus_tracker.start_new_trip(bus_id=bus_id)
                                current_trip = bus_tracker.get_current_trip(bus_id=bus_id)
                                print(f"trip started: {current_trip.get('trip_id') if current_trip else 'NONE'}", flush=True)
                        else:
                            # Outside ESP32 trip time - end active trip if exists
                            if current_trip and current_trip.get('status') == 'active':
                                print(f"AUTO-END: Trip ended for {bus_id} - outside ESP32 schedule {esp32_trip_start}-{esp32_trip_end}", flush=True)
                                # FIXED: Pass bus_id to end trip for the correct bus
                                bus_tracker.end_current_trip(bus_id=bus_id)
                                current_trip = None
                    except Exception as inner_e:
                        print(f"CRASH in auto-mgmt logic: {inner_e}", flush=True)
                        import traceback
                        traceback.print_exc()
                        raise inner_e # Re-raise to be caught by outer handler
                else:
                    # Fallback to existing logic without ESP32 schedule
                    current_trip = bus_tracker.get_current_trip(bus_id=bus_id)
                    esp32_trip_start = "06:00"  # Default
                    esp32_trip_end = "18:00"    # Default
                
                # Build response
                if current_trip and current_trip.get('trip_id'):
                    # Active trip
                    response = {
                        "trip_id": current_trip['trip_id'],
                        "route_name": current_trip.get('route_name', 'Jaffna-Colombo'),
                        "departure_city": current_trip.get('departure_city', 'Colombo'),
                        "destination_city": current_trip.get('destination_city', 'Jaffna'),
                        "schedule_start": esp32_trip_start,
                        "schedule_end": esp32_trip_end,
                        "trip_active": True,
                        "trip_status": "active",
                        "trip_date": datetime.now().strftime("%Y-%m-%d"),
                        "bus_id": bus_id,
                        "current_time": current_time.strftime("%H:%M:%S"),
                        "passengers_inside": current_trip.get('passengers_inside', 0),
                        "duration_minutes": current_trip.get('duration_minutes', 0),
                        "auto_managed": True if esp32_trip_start and esp32_trip_end else False
                    }
                else:
                    # No active trip
                    response = {
                        "trip_id": f"WAITING_{bus_id}_{datetime.now().strftime('%Y%m%d')}",
                        "route_name": "Jaffna-Colombo",
                        "departure_city": "Colombo",
                        "destination_city": "Jaffna", 
                        "schedule_start": esp32_trip_start,
                        "schedule_end": esp32_trip_end,
                        "trip_active": False,
                        "trip_status": "waiting_for_schedule",
                        "trip_date": datetime.now().strftime("%Y-%m-%d"),
                        "bus_id": bus_id,
                        "current_time": current_time.strftime("%H:%M:%S"),
                        "passengers_inside": 0,
                        "next_departure": esp32_trip_start,
                        "auto_managed": True if esp32_trip_start and esp32_trip_end else False
                    }
                
                print(f"Trip context: {response['trip_status']} | Auto: {response.get('auto_managed', False)}")
                self._send_json_response(response)
            except Exception as e:
                import traceback
                print(f"ERROR in trip-context: {e}")
                traceback.print_exc()
                self._send_error_response(f"Trip context error: {str(e)}", 500)
        
        # ESP32 Power Config Endpoint
        elif self.path.startswith('/api/power-config'):
            try:
                query_params = self._get_query_params()
                bus_id = query_params.get('bus_id', [None])[0]
                
                if not bus_id:
                    self._send_error_response('bus_id parameter required', 400)
                    return
                
                config = get_power_config(bus_id)
                if config:
                    response = {
                        "bus_id": config['bus_id'],
                        "deep_sleep_enabled": config.get('deep_sleep_enabled', True),
                        "trip_start": config.get('trip_start', '00:00'),
                        "trip_end": config.get('trip_end', '23:59'),
                        "smart_power_enabled": config.get('smart_power_enabled', False),
                        "trip_windows": config.get('trip_windows', []),
                        "maintenance_interval": config.get('maintenance_interval', 5),
                        "maintenance_duration": config.get('maintenance_duration', 3),
                        "boards": config.get('boards', []),
                        "last_updated": config['last_updated'].isoformat() if isinstance(config.get('last_updated'), datetime) else config.get('last_updated'),
                        "current_server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                    self._send_json_response(response)
                else:
                    self._send_error_response('Failed to get config', 404)
            except Exception as e:
                self._send_error_response(str(e))
        
        # All other endpoints removed - use Node.js backend
        else:
            self._send_error_response('Endpoint not found. Use Node.js backend for frontend APIs.', 404)
    
    def do_POST(self):
        """Handle POST requests - ESP32 ENDPOINTS ONLY"""
        try:
            parsed_path = urlparse(self.path)
            
            # ESP32 Face Embedding Extraction Endpoint
            if parsed_path.path == '/api/extract-face-embedding':
                content_length = int(self.headers.get('Content-Length', 0))
                print(f"📸 Incoming face photo: {content_length / 1024:.1f} KB")
                
                # Read data
                post_data = self.rfile.read(content_length)
                print(f"📥 Data received, parsing JSON...")
                
                data = json.loads(post_data.decode('utf-8'))
                image_data = data.get('image_data', '')
                print(f"🔍 Starting face extraction...")
                
                # Process using the pre-loaded helper
                try:
                    result = extract_face_embedding_from_base64(image_data)
                except ImportError:
                    print("⚠️ face_recognition_helper not found, using fallback")
                    # Fallback to mock embedding
                    import hashlib
                    image_hash = hashlib.md5(image_data.encode()).hexdigest()
                    mock_embedding = [float(int(image_hash[i:i+2], 16)) / 255.0 for i in range(0, 32, 2)]
                    mock_embedding = mock_embedding * 8
                    
                    result = {
                        "success": True,
                        "face_embedding": mock_embedding,
                        "embedding_size": len(mock_embedding),
                        "num_faces": 1,
                        "message": "MOCK embedding (face_recognition_helper not found)",
                        "is_mock": True
                    }
                
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                
                response = {
                    **result,
                    "timestamp": datetime.now().isoformat()
                }
                self.wfile.write(json.dumps(response, indent=2).encode())
                return
            
            # ESP32 Device Health Endpoint
            elif parsed_path.path == '/api/device-health':
                content_length = int(self.headers.get('Content-Length', 0))
                post_data = self.rfile.read(content_length)
                
                data = json.loads(post_data.decode('utf-8'))
                device_id = data.get('device_id', 'UNKNOWN')
                bus_id = data.get('bus_id', 'UNKNOWN')
                
                print(f"💊 ESP32 Health report from {device_id} ({bus_id})")
                
                # Print key health metrics
                if 'health' in data:
                    health = data['health']
                    print(f"   📶 WiFi: {health.get('wifi_status', False)} (RSSI: {health.get('wifi_rssi', 0)})")
                    print(f"   📷 Camera: {health.get('camera_status', False)}")
                    print(f"   🛰️ GPS: {health.get('gps_status', False)} ({health.get('gps_satellite_count', 0)} sats)")
                    print(f"   💾 Memory: {health.get('free_heap_bytes', 0):,} bytes free")
                
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                
                response = {
                    "status": "received",
                    "message": "Health report stored successfully",
                    "device_id": device_id,
                    "timestamp": datetime.now().isoformat()
                }
                self.wfile.write(json.dumps(response, indent=2).encode())
            
            # ESP32 Face Detection Endpoints (Entry/Exit)
            elif parsed_path.path in ['/api/entry-logs', '/api/exit-logs', '/api/face-logs']:
                content_length = int(self.headers.get('Content-Length', 0))
                post_data = self.rfile.read(content_length)
                
                json_data = json.loads(post_data.decode('utf-8'))
                device_id = json_data.get('device_id', 'unknown')
                bus_id = json_data.get('bus_id', bus_tracker.default_bus_id)  # MULTI-BUS: Extract bus_id
                logs = json_data.get('logs', [])
                
                # Determine location type from endpoint
                if parsed_path.path == '/api/entry-logs':
                    location_type = 'ENTRY'
                elif parsed_path.path == '/api/exit-logs':
                    location_type = 'EXIT'
                else:
                    location_type = logs[0].get('location_type', 'UNKNOWN') if logs else 'UNKNOWN'
                
                print(f"\n🚌 ESP32 Face Detection Data Received")
                print(f"Bus: {bus_id}")  # MULTI-BUS: Show bus_id
                print(f"Device: {device_id}")
                print(f"Type: {location_type}")
                print(f"Logs: {len(logs)}")
                
                results = []
                for i, log in enumerate(logs):
                    # Add location_type and bus_id to the log entry
                    log['location_type'] = location_type
                    log['bus_id'] = bus_id  # MULTI-BUS: Add bus_id to each log
                    
                    print(f"\n📍 Processing: {location_type} on {bus_id} - Face ID: {log.get('face_id')}")
                    
                    # --- VALIDATION: Bus ID and Time ---
                    # 1. Validate Bus ID
                    if not bus_tracker.power_configs.find_one({"bus_id": bus_id}):
                        print(f"⚠️ REJECTING: Unknown Bus ID {bus_id}")
                        results.append({"action": "rejected", "message": "Unknown Bus ID"})
                        continue

                    # 2. Validate Time (Prevent 1970/default dates)
                    log_time_str = log.get('timestamp')
                    parsed_time = bus_tracker._parse_timestamp_safe(log_time_str)
                    if parsed_time.year < 2024:
                        print(f"⚠️ REJECTING: Invalid timestamp {parsed_time} (System time not synced?)")
                        results.append({"action": "rejected", "message": "Invalid Timestamp"})
                        continue
                        
                    # 3. Validate Trip Window (STRICT SCHEDULE CHECK)
                    # REQ: Store ONLY if bus_id schedule time is correct
                    is_in_window = False
                    if schedule_manager:
                        # Fetch the dynamic schedule for this specific bus
                        schedule_doc = schedule_manager.bus_schedules.find_one({
                            "bus_id": bus_id,
                            "active": True
                        })
                        
                        if schedule_doc:
                            # Convert UTC log time to Local Time for comparison with Local Schedule
                            local_time = parsed_time + timedelta(hours=bus_tracker.timezone_offset_hours)
                            log_time_hhmm = local_time.strftime("%H:%M")
                            today_name = local_time.strftime('%A').lower()
                            
                            print(f"⏰ Checking schedule: Log UTC {parsed_time.strftime('%H:%M')} -> Local {log_time_hhmm} vs trips")
                            
                            for trip in schedule_doc.get('trips', []):
                                if not trip.get('active', True): continue
                                
                                # Check if log is on a scheduled day
                                days = [d.lower() for d in trip.get('days_of_week', [])]
                                # Default to all days if days_of_week is empty or missing
                                if days and today_name not in days: continue
                                
                                # Get window: Boarding Start -> Arrival + Stop Duration
                                start_t = trip.get('boarding_start_time', '06:00')
                                arrival_t = trip.get('estimated_arrival_time', start_t)
                                stop_mins = trip.get('stop_duration_minutes', 30)
                                
                                try:
                                    arrival_dt = datetime.strptime(arrival_t, "%H:%M")
                                    end_dt = arrival_dt + timedelta(minutes=stop_mins)
                                    end_t = end_dt.strftime("%H:%M")
                                    
                                    # Use cross-day aware comparison
                                    if bus_tracker.is_within_trip_schedule(log_time_hhmm, start_t, end_t):
                                        is_in_window = True
                                        break
                                except Exception as e:
                                    print(f"⚠️ Error parsing schedule window for {bus_id}: {e}")
                                    continue
                        else:
                            print(f"⚠️ No active schedule found in MongoDB for bus {bus_id}")
                    
                    if not is_in_window:
                        check_time = local_time.strftime('%H:%M') if 'local_time' in locals() else parsed_time.strftime('%H:%M')
                        
                        if bus_tracker.debug_allow_all_logs:
                            print(f"🛠️ DEBUG BYPASS: Log for {bus_id} at {check_time} (Local) accepted despite schedule.")
                            is_in_window = True
                        else:
                            print(f"❌ REJECTING: Log for {bus_id} at {check_time} (Local) is OUTSIDE scheduled trip hours.")
                            results.append({"action": "rejected", "message": f"Outside Scheduled Trip Hours (Log:{check_time})"})
                            continue
                    
                    print(f"✅ Log accepted: {bus_id} time is within scheduled window")
                    # -----------------------------------

                    # Process using existing system
                    result = bus_tracker.process_face_log(log)
                    results.append(result)
                    
                    # Print details
                    face_id = log.get('face_id', 'UNKNOWN')
                    timestamp = log.get('timestamp', 'UNKNOWN')
                    lat = log.get('latitude', 0)
                    lon = log.get('longitude', 0)
                    
                    print(f"   Face {i+1}: ID={face_id}, Time={timestamp}")
                    if lat != 0 or lon != 0:
                        print(f"           GPS: {lat:.6f}, {lon:.6f}")
                    
                    # Print processing result
                    if result.get('action') == 'matched_journey':
                        print(f"           ✅ {result['message']}")
                    elif result.get('action') == 'stored_entry':
                        print(f"           📝 {result['message']}")
                    elif result.get('action') == 'unmatched_exit':
                        print(f"           ❌ {result['message']}")
                
                # Send response
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                
                # Return summary
                matched_journeys = len([r for r in results if r.get('action') == 'matched_journey'])
                stored_entries = len([r for r in results if r.get('action') == 'stored_entry'])
                unmatched_exits = len([r for r in results if r.get('action') == 'unmatched_exit'])
                
                response = {
                    "status": "received",
                    "message": f"Processed {len(logs)} {location_type.lower()} logs for {bus_id}",
                    "log_count": len(logs),
                    "bus_id": bus_id,  # MULTI-BUS: Include bus_id in response
                    "device_id": device_id,
                    "processing_summary": {
                        "matched_journeys": matched_journeys,
                        "stored_entries": stored_entries,
                        "unmatched_exits": unmatched_exits
                    },
                    "results": results,
                    "timestamp": datetime.now().isoformat()
                }
                self.wfile.write(json.dumps(response, indent=2).encode())
            
            # ESP32 Board Heartbeat - Restored (Required for POWER_SYNC)
            elif parsed_path.path == '/api/board-heartbeat':
                content_length = int(self.headers.get('Content-Length', 0))
                post_data = self.rfile.read(content_length)
                
                try:
                    data = json.loads(post_data.decode('utf-8'))
                    device_id = data.get('device_id', 'UNKNOWN')
                    target_bus_id = data.get('bus_id', bus_tracker.default_bus_id)
                    
                    print(f"💓 Heartbeat received from {device_id} (Bus: {target_bus_id})")
                    
                    # Update DB with board status
                    try:
                        client_ip = self.client_address[0]
                        device_type = 'ENTRANCE' if 'ENTRANCE' in device_id.upper() else 'EXIT'
                        
                        # Use UTC time for consistent timezone handling
                        utc_now = datetime.now(timezone.utc)
                        
                        # 1. Try to update existing board in the array
                        result = bus_tracker.power_configs.update_one(
                            {"bus_id": target_bus_id, "boards.device_id": device_id},
                            {
                                "$set": {
                                    "boards.$.last_seen": utc_now,
                                    "boards.$.status": "online",
                                    "boards.$.ip_address": client_ip,
                                    "boards.$.location": device_type,
                                    "updated_at": utc_now
                                }
                            }
                        )
                        
                        # 2. If not found (matched_count == 0), push new board
                        if result.matched_count == 0:
                            print(f"➕ Registering new board: {device_id}")
                            bus_tracker.power_configs.update_one(
                                {"bus_id": target_bus_id},
                                {
                                    "$push": {
                                        "boards": {
                                            "device_id": device_id,
                                            "location": device_type,
                                            "ip_address": client_ip,
                                            "last_seen": utc_now,
                                            "status": "online",
                                            "added_at": utc_now
                                        }
                                    },
                                    "$set": {"updated_at": utc_now}
                                },
                                upsert=True
                            )
                            
                    except Exception as db_err:
                        print(f"⚠️ Failed to update board status in DB: {db_err}")

                    self.send_response(200)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()
                    response = {"status": "success", "timestamp": datetime.now().isoformat()}
                    self.wfile.write(json.dumps(response).encode())
                    return
                except Exception as e:
                    print(f"❌ Error processing heartbeat: {e}")
                    self.send_response(400)
                    self.end_headers()
                    return

            else:
                self.send_response(404)
                self.end_headers()
                
        except Exception as e:
            print(f"❌ Error processing request: {e}")
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            error_response = {'status': 'error', 'message': str(e)}
            self.wfile.write(json.dumps(error_response).encode())
    
    # DELETE handler removed - Not needed for ESP32 endpoints
    # All CRUD operations moved to Node.js backend

def run_server(port=None):
    """Run the ESP32 processing backend"""
    # Use environment variable PORT for production deployment
    if port is None:
        port = int(os.environ.get('PORT', 8888))
        
    server_address = ('0.0.0.0', port)  # Bind to 0.0.0.0 for external access
    httpd = ThreadingHTTPServer(server_address, SimplifiedHandler)
    
    print(f"\n{'='*70}")
    print(f"🚌 Python Backend - ESP32 Processing Engine (MULTI-BUS ENABLED)")
    print(f"{'='*70}")
    print(f"📍 Default Bus: {bus_tracker.default_bus_id} ({bus_tracker.route_name})")
    print(f"🔄 Multi-bus support: Accepts bus_id from ESP32 requests")
    print(f"🌐 Server running on port {port}")
    print(f"Press Ctrl+C to stop the server")
    print(f"{'='*70}\n")
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print(f"\n🛑 Server stopped")
        httpd.server_close()

if __name__ == '__main__':
    run_server()