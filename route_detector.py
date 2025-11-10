#!/usr/bin/env python3
"""
Route Detection System
Automatically detects which route a bus is on based on GPS coordinates
"""

import math
from datetime import datetime

class RouteDetector:
    def __init__(self, db):
        self.db = db
        self.routes_collection = db['routes']
        self.route_cache = {}
        self.load_routes()
    
    def load_routes(self):
        """Load all routes from database"""
        try:
            routes = list(self.routes_collection.find({"is_active": True}))
            self.route_cache = {route['route_id']: route for route in routes}
            print(f"✅ Loaded {len(self.route_cache)} active routes")
        except Exception as e:
            print(f"❌ Error loading routes: {e}")
            self.route_cache = {}
    
    def calculate_distance(self, lat1, lon1, lat2, lon2):
        """Calculate distance between two GPS points (Haversine formula)"""
        try:
            R = 6371  # Earth radius in km
            
            lat1, lon1, lat2, lon2 = map(math.radians, [float(lat1), float(lon1), float(lat2), float(lon2)])
            
            dlat = lat2 - lat1
            dlon = lon2 - lon1
            
            a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
            c = 2 * math.asin(math.sqrt(a))
            
            return R * c
        except Exception as e:
            print(f"❌ Error calculating distance: {e}")
            return float('inf')
    
    def is_point_near_route(self, lat, lon, route, threshold_km=2.0):
        """
        Check if GPS point is near any waypoint on the route
        
        Args:
            lat, lon: Current GPS coordinates
            route: Route object with waypoints
            threshold_km: Maximum distance from route (default 2km)
        
        Returns:
            bool: True if point is near route
            dict: Nearest waypoint info
        """
        try:
            waypoints = route.get('waypoints', [])
            
            if not waypoints:
                return False, None
            
            min_distance = float('inf')
            nearest_waypoint = None
            
            for waypoint in waypoints:
                wp_lat = waypoint.get('latitude')
                wp_lon = waypoint.get('longitude')
                
                if wp_lat is None or wp_lon is None:
                    continue
                
                distance = self.calculate_distance(lat, lon, wp_lat, wp_lon)
                
                if distance < min_distance:
                    min_distance = distance
                    nearest_waypoint = waypoint
            
            is_near = min_distance <= threshold_km
            
            return is_near, {
                'distance_km': round(min_distance, 2),
                'waypoint': nearest_waypoint,
                'threshold_km': threshold_km
            }
            
        except Exception as e:
            print(f"❌ Error checking point near route: {e}")
            return False, None
    
    def detect_route_from_gps(self, lat, lon, threshold_km=2.0):
        """
        Detect which route(s) the GPS coordinates belong to
        
        Args:
            lat, lon: Current GPS coordinates
            threshold_km: Maximum distance from route
        
        Returns:
            list: Matching routes with confidence scores
        """
        try:
            matching_routes = []
            
            for route_id, route in self.route_cache.items():
                is_near, info = self.is_point_near_route(lat, lon, route, threshold_km)
                
                if is_near:
                    # Calculate confidence (closer = higher confidence)
                    distance = info['distance_km']
                    confidence = max(0, 1 - (distance / threshold_km))
                    
                    matching_routes.append({
                        'route_id': route_id,
                        'route_name': route.get('route_name'),
                        'from_location': route.get('from_location'),
                        'to_location': route.get('to_location'),
                        'distance_km': distance,
                        'confidence': round(confidence, 2),
                        'nearest_waypoint': info['waypoint']
                    })
            
            # Sort by confidence (highest first)
            matching_routes.sort(key=lambda x: x['confidence'], reverse=True)
            
            return matching_routes
            
        except Exception as e:
            print(f"❌ Error detecting route: {e}")
            return []
    
    def get_best_route(self, lat, lon, threshold_km=2.0):
        """
        Get the most likely route for given GPS coordinates
        
        Returns:
            dict: Best matching route or None
        """
        matching_routes = self.detect_route_from_gps(lat, lon, threshold_km)
        
        if matching_routes:
            best_route = matching_routes[0]
            print(f"🗺️ Detected route: {best_route['route_name']} (confidence: {best_route['confidence']})")
            return best_route
        else:
            print(f"⚠️ No route found near GPS: {lat}, {lon}")
            return None
    
    def is_journey_on_route(self, entry_lat, entry_lon, exit_lat, exit_lon, route_id, threshold_km=2.0):
        """
        Check if a journey (entry to exit) is on a specific route
        
        Returns:
            bool: True if both entry and exit are on the route
            dict: Details about the match
        """
        try:
            if route_id not in self.route_cache:
                return False, {'error': 'Route not found'}
            
            route = self.route_cache[route_id]
            
            # Check entry point
            entry_near, entry_info = self.is_point_near_route(entry_lat, entry_lon, route, threshold_km)
            
            # Check exit point
            exit_near, exit_info = self.is_point_near_route(exit_lat, exit_lon, route, threshold_km)
            
            is_on_route = entry_near and exit_near
            
            return is_on_route, {
                'entry_match': entry_near,
                'entry_distance_km': entry_info['distance_km'] if entry_info else None,
                'exit_match': exit_near,
                'exit_distance_km': exit_info['distance_km'] if exit_info else None,
                'route_name': route.get('route_name')
            }
            
        except Exception as e:
            print(f"❌ Error checking journey on route: {e}")
            return False, {'error': str(e)}
    
    def find_matching_season_ticket_routes(self, entry_lat, entry_lon, exit_lat, exit_lon, member_valid_routes):
        """
        Check if journey matches any of the member's valid routes
        Supports PARTIAL routes (e.g., Jaffna-Kodikamam on Jaffna-Colombo bus)
        
        Args:
            entry_lat, entry_lon: Entry GPS coordinates
            exit_lat, exit_lon: Exit GPS coordinates
            member_valid_routes: List of valid routes from season ticket member
        
        Returns:
            bool: True if journey matches any valid route
            dict: Matching route details
        """
        try:
            # Detect route from entry point
            entry_routes = self.detect_route_from_gps(entry_lat, entry_lon)
            
            # Detect route from exit point
            exit_routes = self.detect_route_from_gps(exit_lat, exit_lon)
            
            if not entry_routes or not exit_routes:
                # Fallback: Check if entry/exit locations match member's valid locations
                return self._check_location_proximity(entry_lat, entry_lon, exit_lat, exit_lon, member_valid_routes)
            
            # Find common routes (entry and exit on same route)
            entry_route_ids = {r['route_id'] for r in entry_routes}
            exit_route_ids = {r['route_id'] for r in exit_routes}
            common_route_ids = entry_route_ids & exit_route_ids
            
            if not common_route_ids:
                # Try location proximity check
                return self._check_location_proximity(entry_lat, entry_lon, exit_lat, exit_lon, member_valid_routes)
            
            # Check if any common route matches member's valid routes
            for valid_route in member_valid_routes:
                route_patterns = valid_route.get('route_patterns', [])
                from_loc = valid_route.get('from_location', '').lower()
                to_loc = valid_route.get('to_location', '').lower()
                
                for route_id in common_route_ids:
                    route = self.route_cache.get(route_id)
                    if not route:
                        continue
                    
                    route_name = route.get('route_name', '')
                    
                    # Check if route matches any pattern
                    if not route_patterns:
                        # IMPROVED: Check if passenger's journey is WITHIN the bus route
                        # Example: Passenger has Jaffna-Kodikamam, Bus is Jaffna-Colombo
                        # This should be valid because Kodikamam is between Jaffna and Colombo
                        
                        # Check if entry location matches
                        entry_match = from_loc in route_name.lower()
                        
                        # Check if exit location is on the route (even if not the final destination)
                        exit_match = to_loc in route_name.lower()
                        
                        if entry_match and exit_match:
                            return True, {
                                'matched_route': route_name,
                                'route_id': route_id,
                                'match_type': 'location_name',
                                'note': 'Exact match'
                            }
                        
                        # NEW: Check if passenger's journey is a SUBSET of the bus route
                        # If entry point matches and exit is anywhere on the route
                        if entry_match:
                            # Check if exit location is within reasonable distance
                            is_valid, proximity_info = self._check_location_proximity(
                                entry_lat, entry_lon, exit_lat, exit_lon, [valid_route]
                            )
                            if is_valid:
                                return True, {
                                    'matched_route': route_name,
                                    'route_id': route_id,
                                    'match_type': 'partial_route',
                                    'note': f'Partial route: {from_loc} → {to_loc} is valid on {route_name}'
                                }
                    else:
                        # Check against patterns
                        for pattern in route_patterns:
                            if pattern.lower() in route_name.lower() or route_name.lower() in pattern.lower():
                                return True, {
                                    'matched_route': route_name,
                                    'route_id': route_id,
                                    'pattern': pattern,
                                    'match_type': 'pattern'
                                }
            
            return False, {
                'reason': 'Journey route not in member valid routes',
                'detected_routes': [self.route_cache[rid].get('route_name') for rid in common_route_ids]
            }
            
        except Exception as e:
            print(f"❌ Error finding matching routes: {e}")
            return False, {'error': str(e)}
    
    def _check_location_proximity(self, entry_lat, entry_lon, exit_lat, exit_lon, member_valid_routes):
        """
        Check if entry/exit locations are close to member's valid route locations
        Uses bus route stops from database to validate partial routes
        
        Example: Season ticket for Chavakachcheri-Kilinochchi
        - Bus route: Jaffna → Chavakachcheri → Kilinochchi → Colombo
        - Entry near Chavakachcheri ✓
        - Exit near Kilinochchi ✓
        - Both are on same route in correct order ✓
        - Result: Valid (FREE)
        """
        import math
        
        def haversine_distance(lat1, lon1, lat2, lon2):
            """Calculate distance in km between two GPS points"""
            R = 6371  # Earth radius in km
            lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
            dlat = lat2 - lat1
            dlon = lon2 - lon1
            a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
            c = 2 * math.asin(math.sqrt(a))
            return R * c
        
        PROXIMITY_THRESHOLD_KM = 10  # Within 10km is considered "at" the stop
        
        # Try to get route stops from database
        try:
            routes_with_stops = list(self.db['busRoutes'].find({'is_active': True}))
            
            if routes_with_stops:
                print(f"   📍 Checking against {len(routes_with_stops)} routes with stops")
                
                for route in routes_with_stops:
                    stops = route.get('stops', [])
                    if not stops:
                        continue
                    
                    # Find which stops the entry and exit are near
                    entry_stop = None
                    exit_stop = None
                    
                    for stop in stops:
                        stop_lat = stop.get('latitude')
                        stop_lon = stop.get('longitude')
                        if not stop_lat or not stop_lon:
                            continue
                        
                        # Check entry proximity
                        if not entry_stop:
                            dist = haversine_distance(entry_lat, entry_lon, stop_lat, stop_lon)
                            if dist <= PROXIMITY_THRESHOLD_KM:
                                entry_stop = stop
                        
                        # Check exit proximity
                        if not exit_stop:
                            dist = haversine_distance(exit_lat, exit_lon, stop_lat, stop_lon)
                            if dist <= PROXIMITY_THRESHOLD_KM:
                                exit_stop = stop
                    
                    # If both entry and exit found on this route
                    if entry_stop and exit_stop:
                        entry_order = entry_stop.get('stop_order', 0)
                        exit_order = exit_stop.get('stop_order', 0)
                        
                        # Check if they're in correct order (entry before exit)
                        if entry_order < exit_order:
                            # Now check if this matches member's valid routes
                            for valid_route in member_valid_routes:
                                from_loc = valid_route.get('from_location', '').lower()
                                to_loc = valid_route.get('to_location', '').lower()
                                
                                entry_name = entry_stop.get('stop_name', '').lower()
                                exit_name = exit_stop.get('stop_name', '').lower()
                                
                                # Check if entry/exit match member's from/to locations
                                if from_loc in entry_name and to_loc in exit_name:
                                    return True, {
                                        'match_type': 'route_stops',
                                        'route_name': route.get('route_name'),
                                        'entry_stop': entry_stop.get('stop_name'),
                                        'exit_stop': exit_stop.get('stop_name'),
                                        'entry_order': entry_order,
                                        'exit_order': exit_order,
                                        'note': f'Valid partial route: {entry_stop.get("stop_name")} → {exit_stop.get("stop_name")}'
                                    }
        except Exception as e:
            print(f"   ⚠️ Error checking route stops: {e}")
            # Fall back to simple proximity check
        
        # Known location coordinates (you can expand this)
        known_locations = {
            'jaffna': (9.6615, 80.0255),
            'kodikamam': (9.6833, 80.0833),
            'chavakachcheri': (9.6667, 80.1667),
            'kilinochchi': (9.3833, 80.4000),
            'vavuniya': (8.7542, 80.4982),
            'anuradhapura': (8.3114, 80.4037),
            'kurunegala': (7.4863, 80.3623),
            'colombo': (6.9271, 79.8612),
            'kandy': (7.2906, 80.6337)
        }
        
        PROXIMITY_THRESHOLD_KM = 10  # Within 10km is considered "at" the location
        
        for valid_route in member_valid_routes:
            from_loc = valid_route.get('from_location', '').lower()
            to_loc = valid_route.get('to_location', '').lower()
            
            # Get coordinates for from/to locations
            from_coords = known_locations.get(from_loc)
            to_coords = known_locations.get(to_loc)
            
            if not from_coords or not to_coords:
                continue
            
            # Check if entry is near "from" location
            entry_distance = haversine_distance(entry_lat, entry_lon, from_coords[0], from_coords[1])
            
            # Check if exit is near "to" location
            exit_distance = haversine_distance(exit_lat, exit_lon, to_coords[0], to_coords[1])
            
            print(f"   📍 Proximity check: {from_loc} → {to_loc}")
            print(f"      Entry distance from {from_loc}: {entry_distance:.2f} km")
            print(f"      Exit distance from {to_loc}: {exit_distance:.2f} km")
            
            if entry_distance <= PROXIMITY_THRESHOLD_KM and exit_distance <= PROXIMITY_THRESHOLD_KM:
                return True, {
                    'match_type': 'location_proximity',
                    'from_location': from_loc,
                    'to_location': to_loc,
                    'entry_distance_km': round(entry_distance, 2),
                    'exit_distance_km': round(exit_distance, 2),
                    'note': f'Journey within valid route: {from_loc} → {to_loc}'
                }
        
        return False, {
            'reason': 'Entry/exit locations not within proximity of valid routes',
            'threshold_km': PROXIMITY_THRESHOLD_KM
        }


# Test function
if __name__ == '__main__':
    print("🧪 Testing Route Detector...")
    
    # Mock database
    class MockDB:
        def __init__(self):
            self.routes = {
                'routes': [
                    {
                        'route_id': 'ROUTE_001',
                        'route_name': 'Jaffna-Colombo',
                        'from_location': 'Jaffna',
                        'to_location': 'Colombo',
                        'is_active': True,
                        'waypoints': [
                            {'name': 'Jaffna', 'latitude': 9.6615, 'longitude': 80.0255},
                            {'name': 'Kodikamam', 'latitude': 9.7615, 'longitude': 80.1255},
                            {'name': 'Vavuniya', 'latitude': 8.7542, 'longitude': 80.4982},
                            {'name': 'Colombo', 'latitude': 6.9271, 'longitude': 79.8612}
                        ]
                    }
                ]
            }
        
        def __getitem__(self, key):
            return self
        
        def find(self, query):
            return self.routes['routes']
    
    mock_db = MockDB()
    detector = RouteDetector(mock_db)
    
    # Test 1: Detect route from GPS
    print("\nTest 1: Detect route from GPS near Jaffna")
    result = detector.get_best_route(9.6615, 80.0255)
    print(f"Result: {result}")
    
    # Test 2: Check if journey is on route
    print("\nTest 2: Check journey Jaffna → Kodikamam")
    is_on_route, info = detector.is_journey_on_route(
        9.6615, 80.0255,  # Jaffna
        9.7615, 80.1255,  # Kodikamam
        'ROUTE_001'
    )
    print(f"On route: {is_on_route}")
    print(f"Info: {info}")
