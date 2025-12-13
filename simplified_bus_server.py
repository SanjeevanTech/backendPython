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
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from sklearn.metrics.pairwise import cosine_similarity
from pymongo import MongoClient
from bson import ObjectId

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(__file__))

from utils.dynamic_schedule_manager import DynamicScheduleManager
from route_detector import RouteDetector

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
        
        # Configuration for single bus
        self.bus_id = "BUS_JC_001"
        self.route_name = "Jaffna-Colombo"  # Will be updated automatically
        self.similarity_threshold = 0.7
        self.season_ticket_similarity_threshold = 0.65  # Lower threshold for ESP32 face variations
        self.time_window_hours = 48  # Increased to 48 hours for testing
        
        # Simple prototype schedule system
        self.schedule = {
            "jaffna_to_colombo": {
                "route_name": "Jaffna-Colombo",
                "departure_time": "07:00",    # 7:00 AM (FIXED)
                "departure_city": "Jaffna",
                "destination_city": "Colombo",
                "estimated_duration_hours": 8
            },
            "colombo_to_jaffna": {
                "route_name": "Colombo-Jaffna", 
                "departure_time": "18:00",    # 6:00 PM (FIXED)
                "departure_city": "Colombo",
                "destination_city": "Jaffna",
                "estimated_duration_hours": 8
            }
        }
        
        # Trip session management
        self.current_trip = None  # Current active trip
        self.trip_sessions = None  # Collection to store trip sessions
        
        # Route detection
        self.route_detector = None  # Will be initialized after database connection
        
        # Distance calculation configuration
        self.distance_api_config = {
            'provider': 'osrm',  # Options: 'osrm', 'openrouteservice', 'mapbox'
            'osrm_base_url': 'http://router.project-osrm.org/route/v1/driving',
            'openrouteservice_api_key': None,  # Set your API key if using ORS
            'openrouteservice_base_url': 'https://api.openrouteservice.org/v2/directions/driving-car',
            'timeout': 10,
            'fallback_to_haversine': True
        }
        
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
            
            # Create indexes
            self.temp_entries.create_index([("bus_id", 1), ("trip_id", 1), ("timestamp", 1)])
            self.final_passengers.create_index([("bus_id", 1), ("trip_id", 1), ("entry_timestamp", 1)])
            self.unmatched_passengers.create_index([("bus_id", 1), ("trip_id", 1), ("timestamp", 1), ("type", 1)])
            self.trip_sessions.create_index([("bus_id", 1), ("trip_id", 1), ("start_time", 1)])
            self.power_configs.create_index([("bus_id", 1)], unique=True)
            self.season_ticket_members.create_index([("member_id", 1)], unique=True)
            self.season_ticket_members.create_index([("is_active", 1), ("valid_from", 1), ("valid_until", 1)])
            
            print("✅ Connected to MongoDB - Simplified Bus Tracking")
            print(f"🚌 Tracking Bus: {self.bus_id} ({self.route_name})")
            print(f"📊 Collections: temp_entries, busPassengerList, unmatchedPassengers, tripSessions, seasonTicketMembers")
            
            # Initialize route detector
            try:
                self.route_detector = RouteDetector(self.db)
                print("✅ Route detector initialized")
            except Exception as e:
                print(f"⚠️ Route detector initialization failed: {e}")
                self.route_detector = None
            
            # Load or create current trip
            self.load_current_trip()
            
        except Exception as e:
            print(f"❌ Failed to connect to MongoDB: {e}")
            raise
    
    def generate_trip_id(self, start_time=None):
        """Generate unique trip ID"""
        if start_time is None:
            start_time = datetime.now()
        date_str = start_time.strftime('%Y-%m-%d')
        time_str = start_time.strftime('%H:%M')
        return f"{self.bus_id}_{date_str}_{time_str}"
    
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
    
    def load_current_trip(self):
        """Load active trip from database or create new one"""
        try:
            # Find active trip
            active_trip = self.trip_sessions.find_one({
                "bus_id": self.bus_id,
                "status": "active"
            })
            
            if active_trip:
                self.current_trip = {
                    'trip_id': active_trip['trip_id'],
                    'start_time': active_trip['start_time'],
                    'status': 'active',
                    '_id': active_trip['_id']
                }
                print(f"📍 Loaded active trip: {self.current_trip['trip_id']}")
            else:
                # Auto-start new trip
                self.start_new_trip()
        except Exception as e:
            print(f"❌ Error loading trip: {e}")
            self.start_new_trip()
    
    def start_new_trip(self, start_time=None, initial_gps=None):
        """Start a new trip session with smart route detection"""
        try:
            if start_time is None:
                start_time = datetime.now()
            
            # End previous trip if exists
            if self.current_trip and self.current_trip.get('status') == 'active':
                self.end_current_trip()
            
            # Cleanup orphaned entries using existing method
            self.cleanup_old_temp_entries(hours_old=0)  # Clean all old entries
            
            # Generate trip ID
            trip_id = self.generate_trip_id(start_time)
            
            # Smart route detection based on GPS
            detected_route = self.route_name  # Default
            if initial_gps and hasattr(self, 'route_detector'):
                route_info = self.route_detector.detect_route_direction(self.bus_id, initial_gps, start_time)
                if route_info:
                    detected_route = route_info['route_name']
                    print(f"🛣️ Auto-detected route: {detected_route}")
            
            # Create trip session record
            trip_session = {
                'trip_id': trip_id,
                'bus_id': self.bus_id,
                'route_name': detected_route,
                'start_time': start_time,
                'end_time': None,
                'status': 'active',
                'total_passengers': 0,
                'total_unmatched': 0,
                'route_detection_gps': initial_gps,
                'created_at': datetime.now()
            }
            
            result = self.trip_sessions.insert_one(trip_session)
            
            # Store current trip info
            self.current_trip = {
                'trip_id': trip_id,
                'start_time': start_time,
                'status': 'active',
                '_id': result.inserted_id
            }
            
            print(f"🚌 Started new trip: {trip_id}")
            return trip_id
        except Exception as e:
            print(f"❌ Error starting trip: {e}")
            return None
    
    def end_current_trip(self):
        """End current trip and move unmatched to unmatched collection"""
        try:
            if not self.current_trip:
                print("❌ No active trip")
                return False
            
            trip_id = self.current_trip['trip_id']
            
            # Count passengers for this trip
            passenger_count = self.final_passengers.count_documents({"trip_id": trip_id})
            
            # Move remaining temp_entries to unmatched (ENTRY type - no exit found)
            remaining = list(self.temp_entries.find({
                "trip_id": trip_id,
                "bus_id": self.bus_id
            }))
            
            print(f"🔍 Found {len(remaining)} unmatched ENTRY records in temp_entries")
            
            unmatched_count = 0
            for entry in remaining:
                unmatched_entry = {
                    "trip_id": trip_id,
                    "bus_id": self.bus_id,
                    "route_name": entry.get('route_name', self.route_name),
                    "type": "ENTRY",  # These are ENTRY faces that never got an EXIT match
                    "trip_start_time": self.current_trip['start_time'],
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
                {"_id": self.current_trip['_id']},
                {
                    "$set": {
                        "status": "completed",
                        "end_time": datetime.now(),
                        "total_passengers": passenger_count,
                        "total_unmatched": unmatched_count
                    }
                }
            )
            
            print(f"✅ Ended trip: {trip_id}")
            print(f"   Passengers: {passenger_count}, Unmatched: {unmatched_count}")
            
            self.current_trip = None
            return True
        except Exception as e:
            print(f"❌ Error ending trip: {e}")
            return False
    
    # Removed: get_current_route_info() - Replaced by DynamicScheduleManager
    # Removed: is_departure_time() - Replaced by DynamicScheduleManager
    
    def get_current_trip(self):
        """Get current trip information"""
        if not self.current_trip:
            return {
                'trip_id': None,
                'bus_id': self.bus_id,
                'route_name': self.route_name,
                'status': 'waiting_for_departure',
                'trip_active': False,
                'passengers_inside': 0,
                'passengers_completed': 0,
                'duration_minutes': 0
            }
        
        try:
            trip_session = self.trip_sessions.find_one({"_id": self.current_trip['_id']})
            
            if trip_session:
                passenger_count = self.final_passengers.count_documents({"trip_id": self.current_trip['trip_id']})
                temp_count = self.temp_entries.count_documents({"trip_id": self.current_trip['trip_id']})
                duration_minutes = (datetime.now() - trip_session['start_time']).total_seconds() / 60
                
                # Simplified status
                if duration_minutes < 60:
                    trip_status = "departing"
                elif duration_minutes < 480:  # 8 hours
                    trip_status = "in_transit"
                else:
                    trip_status = "approaching_destination"
                
                return {
                    'trip_id': self.current_trip['trip_id'],
                    'bus_id': self.bus_id,
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
            print(f"❌ Error getting trip: {e}")
            return None
    
    def get_all_trips(self, limit=10):
        """Get recent trips"""
        try:
            trips = list(self.trip_sessions.find({
                "bus_id": self.bus_id
            }).sort("start_time", -1).limit(limit))
            
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
            
            # FIRST: Try checking ALL active season ticket members (no route filtering)
            # This ensures we don't miss matches due to GPS/route filtering issues
            query_all = {
                "is_active": True,
                "valid_from": {"$lte": now},
                "valid_until": {"$gte": now},
                "face_embedding": {"$exists": True, "$ne": []}
            }
            
            all_active_members = list(self.season_ticket_members.find(query_all))
            
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
        """Store temporary entry for matching"""
        try:
            # Ensure we have an active trip
            if not self.current_trip or self.current_trip.get('status') != 'active':
                print("⚠️ No active trip, starting new trip...")
                self.start_new_trip()
            
            # Check if this is a season ticket member at entry
            # OPTIMIZED: Pass route and GPS for targeted checking
            gps_location = {
                'latitude': log_entry.get('latitude', 0),
                'longitude': log_entry.get('longitude', 0)
            }
            season_member, season_similarity = self.check_season_ticket_member(
                log_entry.get('face_embedding', []),
                bus_route=self.current_trip.get('trip_id'),  # Use trip_id as route identifier
                gps_location=gps_location
            )
            
            season_ticket_detected = None
            if season_member:
                season_ticket_detected = {
                    "member_id": season_member['member_id'],
                    "member_name": season_member['name'],
                    "similarity_score": float(season_similarity)
                }
                print(f"🎫 Season ticket member detected at ENTRY: {season_member['name']}")
            
            temp_entry = {
                "trip_id": self.current_trip['trip_id'],  # NEW!
                "trip_start_time": self.current_trip['start_time'],  # NEW!
                "bus_id": self.bus_id,
                "route_name": self.route_name,
                "face_id": log_entry.get('face_id', 0),
                "face_embedding": log_entry.get('face_embedding', []),
                "embedding_size": log_entry.get('embedding_size', 0),
                "season_ticket_detected": season_ticket_detected,  # NEW!
                "entry_location": {
                    "latitude": log_entry.get('latitude', 0),
                    "longitude": log_entry.get('longitude', 0),
                    "device_id": log_entry.get('device_id'),
                    "timestamp": log_entry.get('timestamp')
                },
                "entry_timestamp": self._parse_timestamp_safe(log_entry.get('timestamp')),
                "created_at": datetime.now()
            }
            
            result = self.temp_entries.insert_one(temp_entry)
            print(f"✅ Stored temporary entry: {result.inserted_id} (Trip: {self.current_trip['trip_id']})")
            return str(result.inserted_id)
            
        except Exception as e:
            print(f"❌ Error storing entry: {e}")
            return None
    
    def find_matching_entry(self, exit_log):
        """Find matching entry and create final passenger record"""
        if not exit_log.get('face_embedding'):
            return None, 0.0
        
        try:
            # Ensure we have an active trip
            if not self.current_trip or self.current_trip.get('status') != 'active':
                print("⚠️ No active trip for exit matching")
                return None, 0.0
            
            # Get recent unmatched entries for CURRENT TRIP ONLY
            time_threshold = datetime.now() - timedelta(hours=self.time_window_hours)
            
            unmatched_entries = self.temp_entries.find({
                "trip_id": self.current_trip['trip_id'],  # NEW! Only match within same trip
                "bus_id": self.bus_id,
                "entry_timestamp": {"$gte": time_threshold},
                "face_embedding": {"$exists": True, "$ne": []}
            }).sort("entry_timestamp", -1)
            
            entries_list = list(unmatched_entries)
            print(f"🔍 After filtering: Found {len(entries_list)} entries matching criteria")
            
            if not entries_list:
                print(f"❌ No unmatched entries found for bus {self.bus_id}")
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
                    bus_route=self.current_trip.get('trip_id'),
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
                    "trip_id": self.current_trip['trip_id'],  # NEW!
                    "trip_start_time": self.current_trip['start_time'],  # NEW!
                    "bus_id": self.bus_id,
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
        """Process incoming face log entry"""
        location_type = log_entry.get('location_type', '').upper()
        
        if location_type == 'ENTRY':
            # Store temporary entry
            entry_id = self.store_entry(log_entry)
            
            if entry_id:
                return {
                    'action': 'stored_entry',
                    'entry_id': entry_id,
                    'bus_id': self.bus_id,
                    'face_id': log_entry.get('face_id', 0),
                    'message': f'Entry stored temporarily for matching (face_id: {log_entry.get("face_id", 0)})'
                }
            else:
                return {
                    'action': 'error',
                    'message': 'Failed to store entry'
                }
        
        elif location_type == 'EXIT':
            # Find matching entry and create final record
            match_result, similarity = self.find_matching_entry(log_entry)
            
            if match_result:
                return {
                    'action': 'matched_journey',
                    'passenger_id': match_result['id'],
                    'bus_id': self.bus_id,
                    'entry_face_id': match_result['entry_face_id'],
                    'exit_face_id': match_result['exit_face_id'],
                    'similarity': float(similarity),
                    'journey_duration': match_result['journey_duration_minutes'],
                    'message': f'✅ Journey completed! Passenger {match_result["id"]} (similarity: {similarity:.3f}, duration: {match_result["journey_duration_minutes"]:.1f} min)'
                }
            else:
                # Store unmatched exit
                unmatched_exit_id = self.store_unmatched_exit(log_entry, similarity)
                return {
                    'action': 'unmatched_exit',
                    'bus_id': self.bus_id,
                    'face_id': log_entry.get('face_id', 0),
                    'best_similarity': float(similarity),
                    'unmatched_id': unmatched_exit_id,
                    'message': f'❌ No matching entry found for exit face_id {log_entry.get("face_id", 0)} (best similarity: {similarity:.3f}) - Stored as unmatched'
                }
        
        else:
            return {
                'action': 'error',
                'message': f'Unknown location_type: {location_type}'
            }
    
    def store_unmatched_exit(self, exit_log, best_similarity):
        """Store unmatched exit passenger"""
        try:
            # Ensure we have an active trip
            if not self.current_trip or self.current_trip.get('status') != 'active':
                print("⚠️ No active trip for unmatched exit")
                return None
            
            unmatched_exit = {
                "trip_id": self.current_trip['trip_id'],  # NEW!
                "trip_start_time": self.current_trip['start_time'],  # NEW!
                "bus_id": self.bus_id,
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
                "created_at": datetime.now()
            }
            
            result = self.unmatched_passengers.insert_one(unmatched_exit)
            print(f"📝 Stored unmatched exit: {result.inserted_id}")
            return str(result.inserted_id)
            
        except Exception as e:
            print(f"❌ Error storing unmatched exit: {e}")
            return None
    
    def cleanup_old_temp_entries(self, hours_old=24):
        """Move old temp entries to unmatched collection"""
        try:
            cutoff_time = datetime.now() - timedelta(hours=hours_old)
            old_entries = list(self.temp_entries.find({
                "bus_id": self.bus_id,
                "entry_timestamp": {"$lt": cutoff_time}
            }))
            
            if old_entries:
                for entry in old_entries:
                    unmatched_entry = {
                        "trip_id": entry.get('trip_id', 'UNKNOWN'),
                        "bus_id": self.bus_id,
                        "route_name": self.route_name,
                        "type": "ENTRY",
                        "trip_start_time": entry.get('trip_start_time'),
                        "face_id": entry.get('face_id', 0),
                        "face_embedding": entry.get('face_embedding', []),
                        "embedding_size": entry.get('embedding_size', 0),
                        "location": entry['entry_location'],
                        "timestamp": entry['entry_timestamp'],
                        "best_similarity_found": 0.0,
                        "reason": f"No exit found within {hours_old} hours",
                        "created_at": datetime.now()
                    }
                    self.unmatched_passengers.insert_one(unmatched_entry)
                
                result = self.temp_entries.delete_many({
                    "bus_id": self.bus_id,
                    "entry_timestamp": {"$lt": cutoff_time}
                })
                
                print(f"🗑️ Cleaned up {result.deleted_count} old temp entries")
                return len(old_entries)
            
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
    
    # Removed: configure_distance_api() - Never called, OSRM is hardcoded
    
    def get_stats(self):
        """Get current statistics"""
        try:
            temp_count = self.temp_entries.count_documents({"bus_id": self.bus_id})
            final_count = self.final_passengers.count_documents({"bus_id": self.bus_id})
            unmatched_count = self.unmatched_passengers.count_documents({"bus_id": self.bus_id})
            unmatched_entries = self.unmatched_passengers.count_documents({"bus_id": self.bus_id, "type": "ENTRY"})
            unmatched_exits = self.unmatched_passengers.count_documents({"bus_id": self.bus_id, "type": "EXIT"})
            
            # Get distance calculation stats
            journeys_with_distance = self.final_passengers.count_documents({
                "bus_id": self.bus_id, 
                "distance_info.success": True
            })
            
            # Perform cleanup of old temp entries
            cleaned_count = self.cleanup_old_temp_entries()
            
            return {
                "bus_id": self.bus_id,
                "route_name": self.route_name,
                "temporary_entries": temp_count,
                "completed_journeys": final_count,
                "journeys_with_distance": journeys_with_distance,
                "unmatched_passengers": unmatched_count,
                "unmatched_entries": unmatched_entries,
                "unmatched_exits": unmatched_exits,
                "passengers_currently_inside": temp_count,
                "cleaned_old_entries": cleaned_count,
                "distance_api_provider": self.distance_api_config['provider']
            }
        except Exception as e:
            print(f"❌ Error getting stats: {e}")
            return {"error": str(e)}

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
            query_params = self._get_query_params()
            bus_id = query_params.get('bus_id', [bus_tracker.bus_id])[0]
            esp32_trip_start = query_params.get('trip_start', [None])[0]
            esp32_trip_end = query_params.get('trip_end', [None])[0]
            
            # Get current time
            current_time = datetime.now()
            current_time_str = current_time.strftime("%H:%M")
            
            # AUTOMATIC TRIP MANAGEMENT based on ESP32 schedule
            if esp32_trip_start and esp32_trip_end:
                print(f"📅 ESP32 provided schedule: {esp32_trip_start} - {esp32_trip_end}")
                
                # Check if current time is within ESP32 trip schedule
                is_esp32_trip_time = bus_tracker.is_within_trip_schedule(current_time_str, esp32_trip_start, esp32_trip_end)
                
                # Get current trip
                current_trip = bus_tracker.get_current_trip()
                
                if is_esp32_trip_time:
                    # Within ESP32 trip time - ensure we have an active trip
                    if not current_trip or current_trip.get('status') != 'active':
                        print(f"🚀 AUTO-START: Creating trip for ESP32 schedule {esp32_trip_start}-{esp32_trip_end}")
                        # Move any old temp_entries to unmatched before starting new trip
                        bus_tracker.cleanup_old_temp_entries_for_new_trip()
                        # Auto-start trip
                        bus_tracker.start_new_trip()
                        current_trip = bus_tracker.get_current_trip()
                else:
                    # Outside ESP32 trip time - end active trip if exists
                    if current_trip and current_trip.get('status') == 'active':
                        print(f"🏁 AUTO-END: Trip ended - outside ESP32 schedule {esp32_trip_start}-{esp32_trip_end}")
                        bus_tracker.end_current_trip()
                        current_trip = None
            else:
                # Fallback to existing logic without ESP32 schedule
                current_trip = bus_tracker.get_current_trip()
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
            
            print(f"📍 Trip context: {response['trip_status']} | Auto: {response.get('auto_managed', False)}")
            self._send_json_response(response)
        
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
                        "last_updated": config['last_updated'].isoformat() if isinstance(config.get('last_updated'), datetime) else config.get('last_updated')
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
                post_data = self.rfile.read(content_length)
                
                data = json.loads(post_data.decode('utf-8'))
                image_data = data.get('image_data', '')
                
                print(f"📸 Face embedding extraction request received")
                
                # Import face recognition helper
                try:
                    from face_recognition_helper import extract_face_embedding_from_base64
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
                logs = json_data.get('logs', [])
                
                # Determine location type from endpoint
                if parsed_path.path == '/api/entry-logs':
                    location_type = 'ENTRY'
                elif parsed_path.path == '/api/exit-logs':
                    location_type = 'EXIT'
                else:
                    location_type = logs[0].get('location_type', 'UNKNOWN') if logs else 'UNKNOWN'
                
                print(f"\n🚌 ESP32 Face Detection Data Received")
                print(f"Device: {device_id}")
                print(f"Type: {location_type}")
                print(f"Logs: {len(logs)}")
                
                results = []
                for i, log in enumerate(logs):
                    # Add location_type to the log entry
                    log['location_type'] = location_type
                    
                    print(f"\n📍 Processing: {location_type} - Face ID: {log.get('face_id')}")
                    
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
                    "message": f"Processed {len(logs)} {location_type.lower()} logs",
                    "log_count": len(logs),
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
                    # Use provided bus_id or default to tracking bus
                    target_bus_id = data.get('bus_id', bus_tracker.bus_id)
                    
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
    httpd = HTTPServer(server_address, SimplifiedHandler)
    
    print(f"\n{'='*70}")
    print(f"🚌 Python Backend - ESP32 Processing Engine Only")
    print(f"{'='*70}")
    print(f"📍 Bus: {bus_tracker.bus_id} ({bus_tracker.route_name})")
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