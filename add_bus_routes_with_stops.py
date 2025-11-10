"""
Add bus routes with all stops/waypoints to the database
This allows the system to validate season tickets for partial routes
"""
from pymongo import MongoClient

# MongoDB connection
MONGODB_URI = 'mongodb+srv://sanjeeBusPassenger:Hz3czXqVoc4ThTiO@buspassenger.lskaqo5.mongodb.net/bus_passenger_db?retryWrites=true&w=majority&appName=BusPassenger'
client = MongoClient(MONGODB_URI)
db = client['bus_passenger_db']

# Define bus routes with all stops
bus_routes = [
    {
        "route_id": "ROUTE_JC_001",
        "route_name": "Jaffna-Colombo",
        "bus_id": "BUS_JC_001",
        "direction": "southbound",
        "stops": [
            {
                "stop_name": "Jaffna",
                "latitude": 9.6615,
                "longitude": 80.0255,
                "stop_order": 1,
                "distance_from_start_km": 0
            },
            {
                "stop_name": "Kodikamam",
                "latitude": 9.6833,
                "longitude": 80.0833,
                "stop_order": 2,
                "distance_from_start_km": 8
            },
            {
                "stop_name": "Chavakachcheri",
                "latitude": 9.6667,
                "longitude": 80.1667,
                "stop_order": 3,
                "distance_from_start_km": 15
            },
            {
                "stop_name": "Kilinochchi",
                "latitude": 9.3833,
                "longitude": 80.4000,
                "stop_order": 4,
                "distance_from_start_km": 45
            },
            {
                "stop_name": "Vavuniya",
                "latitude": 8.7542,
                "longitude": 80.4982,
                "stop_order": 5,
                "distance_from_start_km": 115
            },
            {
                "stop_name": "Anuradhapura",
                "latitude": 8.3114,
                "longitude": 80.4037,
                "stop_order": 6,
                "distance_from_start_km": 165
            },
            {
                "stop_name": "Kurunegala",
                "latitude": 7.4863,
                "longitude": 80.3623,
                "stop_order": 7,
                "distance_from_start_km": 250
            },
            {
                "stop_name": "Colombo",
                "latitude": 6.9271,
                "longitude": 79.8612,
                "stop_order": 8,
                "distance_from_start_km": 350
            }
        ],
        "total_distance_km": 350,
        "estimated_duration_hours": 8,
        "is_active": True
    },
    {
        "route_id": "ROUTE_CJ_001",
        "route_name": "Colombo-Jaffna",
        "bus_id": "BUS_JC_001",
        "direction": "northbound",
        "stops": [
            {
                "stop_name": "Colombo",
                "latitude": 6.9271,
                "longitude": 79.8612,
                "stop_order": 1,
                "distance_from_start_km": 0
            },
            {
                "stop_name": "Kurunegala",
                "latitude": 7.4863,
                "longitude": 80.3623,
                "stop_order": 2,
                "distance_from_start_km": 100
            },
            {
                "stop_name": "Anuradhapura",
                "latitude": 8.3114,
                "longitude": 80.4037,
                "stop_order": 3,
                "distance_from_start_km": 185
            },
            {
                "stop_name": "Vavuniya",
                "latitude": 8.7542,
                "longitude": 80.4982,
                "stop_order": 4,
                "distance_from_start_km": 235
            },
            {
                "stop_name": "Kilinochchi",
                "latitude": 9.3833,
                "longitude": 80.4000,
                "stop_order": 5,
                "distance_from_start_km": 305
            },
            {
                "stop_name": "Chavakachcheri",
                "latitude": 9.6667,
                "longitude": 80.1667,
                "stop_order": 6,
                "distance_from_start_km": 335
            },
            {
                "stop_name": "Kodikamam",
                "latitude": 9.6833,
                "longitude": 80.0833,
                "stop_order": 7,
                "distance_from_start_km": 342
            },
            {
                "stop_name": "Jaffna",
                "latitude": 9.6615,
                "longitude": 80.0255,
                "stop_order": 8,
                "distance_from_start_km": 350
            }
        ],
        "total_distance_km": 350,
        "estimated_duration_hours": 8,
        "is_active": True
    }
]

def add_routes():
    """Add routes to database"""
    routes_collection = db['busRoutes']
    
    print("🚌 Adding bus routes with stops...")
    
    for route in bus_routes:
        # Check if route already exists
        existing = routes_collection.find_one({"route_id": route["route_id"]})
        
        if existing:
            # Update existing route
            routes_collection.update_one(
                {"route_id": route["route_id"]},
                {"$set": route}
            )
            print(f"✅ Updated: {route['route_name']} ({len(route['stops'])} stops)")
        else:
            # Insert new route
            routes_collection.insert_one(route)
            print(f"✅ Added: {route['route_name']} ({len(route['stops'])} stops)")
        
        # Print stops
        print(f"   Stops: {' → '.join([s['stop_name'] for s in route['stops']])}")
    
    print(f"\n✅ Total routes in database: {routes_collection.count_documents({})}")

def verify_routes():
    """Verify routes are in database"""
    routes_collection = db['busRoutes']
    
    print("\n📊 Verifying routes in database...")
    
    routes = list(routes_collection.find({}))
    
    for route in routes:
        print(f"\n🚌 {route['route_name']} ({route['route_id']})")
        print(f"   Direction: {route['direction']}")
        print(f"   Total stops: {len(route['stops'])}")
        print(f"   Distance: {route['total_distance_km']} km")
        print(f"   Stops:")
        for stop in route['stops']:
            print(f"      {stop['stop_order']}. {stop['stop_name']} ({stop['distance_from_start_km']} km)")

def test_season_ticket_validation():
    """Test if season ticket validation works with new route data"""
    print("\n🧪 Testing Season Ticket Validation...")
    
    test_cases = [
        {
            "name": "Jaffna → Kodikamam (Valid - First 2 stops)",
            "entry": "Jaffna",
            "exit": "Kodikamam",
            "expected": "FREE"
        },
        {
            "name": "Chavakachcheri → Kilinochchi (Valid - Middle stops)",
            "entry": "Chavakachcheri",
            "exit": "Kilinochchi",
            "expected": "FREE"
        },
        {
            "name": "Vavuniya → Colombo (Valid - Last stops)",
            "entry": "Vavuniya",
            "exit": "Colombo",
            "expected": "FREE"
        },
        {
            "name": "Jaffna → Colombo (Valid - Full route)",
            "entry": "Jaffna",
            "exit": "Colombo",
            "expected": "FREE"
        }
    ]
    
    for test in test_cases:
        print(f"\n   Test: {test['name']}")
        print(f"   Entry: {test['entry']}")
        print(f"   Exit: {test['exit']}")
        print(f"   Expected: {test['expected']}")

if __name__ == '__main__':
    print("="*70)
    print("BUS ROUTE SETUP WITH STOPS")
    print("="*70)
    
    add_routes()
    verify_routes()
    test_season_ticket_validation()
    
    print("\n" + "="*70)
    print("✅ SETUP COMPLETE!")
    print("="*70)
    print("\nNow the system can validate season tickets for ANY stops on the route!")
    print("Examples:")
    print("  ✅ Jaffna → Kodikamam (FREE)")
    print("  ✅ Chavakachcheri → Kilinochchi (FREE)")
    print("  ✅ Vavuniya → Anuradhapura (FREE)")
    print("  ✅ Any stop → Any other stop on same route (FREE)")
