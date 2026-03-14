#!/usr/bin/env python3
"""
Simplified Bus Passenger Tracking Server
- One bus: BUS_JC_001 (Jaffna-Colombo)
- Temporary storage for matching
- Final collection: busPassengerList
- ESP32 Integration for face detection
"""

import json
import math
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import numpy as np
import requests
from bson import ObjectId
from pymongo import MongoClient
from sklearn.metrics.pairwise import cosine_similarity

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(__file__))

from face_recognition_helper import extract_face_embedding_from_base64
from route_detector import RouteDetector
from utils.dynamic_schedule_manager import DynamicScheduleManager

# ── ESP32 Embedding Constants ─────────────────────────────────────────────────
# MobileFaceNet (FaceRecognition112V1S8 / S16) always outputs 128-dimensional
# embeddings.  The CSV logger in csv_logger.c hard-caps at 128 as well.
EXPECTED_EMBEDDING_DIM = 128

# ── Trip lifecycle constants ──────────────────────────────────────────────────
# After the scheduled window ends, the trip enters the "ending" state.
# During this grace window:
#   • EXIT logs are still accepted so passengers who alight late can be matched.
#   • ENTRY logs are rejected (bus has arrived; no new boardings).
# After TRIP_END_GRACE_MINUTES the background thread calls close_trip_session()
# which moves all remaining temp_entries for THAT trip only → unmatchedPassengers.
TRIP_END_GRACE_MINUTES = 30


def validate_esp32_embedding(embedding, context=""):
    """
    Validate an embedding vector received from an ESP32 device.

    Rules
    -----
    1. Must be a non-empty list / array.
    2. Dimension must equal EXPECTED_EMBEDDING_DIM (128).
    3. Must not contain NaN or Inf values.
    4. L2-norm must be > 0.1  (the ESP-WHO model always produces a unit vector;
       a near-zero norm means the network produced a degenerate output or the
       data was corrupted in transit).

    Returns
    -------
    (True,  "")          – embedding is valid
    (False, reason_str)  – embedding is invalid, reason_str describes the problem
    """
    prefix = f"[{context}] " if context else ""

    if not embedding:
        return False, f"{prefix}Empty embedding"

    dim = len(embedding)
    if dim != EXPECTED_EMBEDDING_DIM:
        return False, (
            f"{prefix}Dimension mismatch: got {dim}, expected {EXPECTED_EMBEDDING_DIM}"
        )

    arr = np.array(embedding, dtype=np.float32)

    if np.any(np.isnan(arr)) or np.any(np.isinf(arr)):
        return False, f"{prefix}Embedding contains NaN or Inf values"

    norm = float(np.linalg.norm(arr))
    if norm < 0.1:
        return False, (
            f"{prefix}Embedding norm too small ({norm:.4f}); likely degenerate/zero vector"
        )

    return True, ""


class SimplifiedBusTracker:
    def __init__(
        self,
        mongo_url="mongodb+srv://sanjeeBusPassenger:Hz3czXqVoc4ThTiO@buspassenger.lskaqo5.mongodb.net/?retryWrites=true&w=majority&appName=BusPassenger",
    ):
        self.mongo_url = mongo_url
        self.client = None
        self.db = None

        # Collections
        self.temp_entries = None  # Temporary storage for unmatched entries
        self.final_passengers = None  # Final collection: busPassengerList
        self.unmatched_passengers = None  # Unmatched passengers collection
        self.power_configs = None  # Power management configurations per bus
        self.season_ticket_members = None  # Season ticket members collection
        self.contractors = None  # Contractor collection

        # Configuration - MULTI-BUS SUPPORT
        self.default_bus_id = "BUS_JC_001"  # Default bus if none specified
        self.bus_id = self.default_bus_id  # For backward compatibility with handler
        self.route_name = "Jaffna-Colombo"  # Will be updated automatically
        # Cross-camera entry→exit threshold for ESP32 S8 (INT8 quantized) embeddings.
        # Both entry and exit embeddings come from ESP-WHO FaceRecognition112V1S8 model.
        # Firmware applies brightness normalization (mean→128) on the 112x112 aligned face crop
        # BEFORE the model runs — this removes AWB/AEC settling differences between the two cameras.
        # After normalization: cross-device same-person similarity ~0.65-0.80 (was ~0.50-0.57).
        # 0.60 is the correct threshold: catches cross-device matches, rejects strangers (< 0.35).
        self.similarity_threshold = 0.60
        self.season_ticket_similarity_threshold = (
            0.60  # Season ticket: same camera, higher confidence needed
        )
        self.time_window_hours = 48  # Increased to 48 hours for testing
        self.timezone_offset_hours = 5.5  # Adjust for Sri Lanka (+5:30)
        self.debug_allow_all_logs = (
            True  # SET TO TRUE FOR TESTING (Accepts logs outside schedule)
        )

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
            self.db = self.client["bus_passenger_db"]

            # Collections
            self.temp_entries = self.db["temp_entries"]  # Temporary unmatched entries
            self.final_passengers = self.db[
                "busPassengerList"
            ]  # Final matched passengers
            self.unmatched_passengers = self.db[
                "unmatchedPassengers"
            ]  # Unmatched passengers
            self.trip_sessions = self.db["tripSessions"]  # Trip session tracking
            self.power_configs = self.db[
                "powerConfigs"
            ]  # Power management configs per bus
            self.season_ticket_members = self.db[
                "seasonTicketMembers"
            ]  # Season ticket members
            self.contractors = self.db["contractors"]  # Contractors

            # Create indexes
            self.temp_entries.create_index(
                [("bus_id", 1), ("trip_id", 1), ("timestamp", 1)]
            )
            self.final_passengers.create_index(
                [("bus_id", 1), ("trip_id", 1), ("entry_timestamp", 1)]
            )
            self.unmatched_passengers.create_index(
                [("bus_id", 1), ("trip_id", 1), ("timestamp", 1), ("type", 1)]
            )
            self.trip_sessions.create_index(
                [("bus_id", 1), ("trip_id", 1), ("start_time", 1)]
            )
            self.power_configs.create_index([("bus_id", 1)], unique=True)
            self.season_ticket_members.create_index([("member_id", 1)], unique=True)
            self.season_ticket_members.create_index(
                [("is_active", 1), ("valid_from", 1), ("valid_until", 1)]
            )
            # ── Contractors index fix ────────────────────────────────────────
            # The old unique=True on bus_id meant only ONE contractor record could
            # exist per bus.  A second insert (Conductor after Driver) silently
            # failed with a duplicate-key error, so only the first staff member
            # was ever stored and compared.  Drop the unique index and replace it
            # with a non-unique bus_id index + a unique compound (bus_id, name).
            try:
                self.contractors.drop_index("bus_id_1")
                print("[OK] Dropped old unique contractors.bus_id index", flush=True)
            except Exception:
                pass  # index didn't exist or was already corrected
            self.contractors.create_index(
                [("bus_id", 1)]
            )  # non-unique: multiple staff per bus
            self.contractors.create_index(
                [("bus_id", 1), ("name", 1)],
                unique=True,  # unique per bus+staff name
            )
            self.contractors.create_index([("is_active", 1)])  # fast active-only filter

            print("[OK] Connected to MongoDB - Multi-Bus Tracking Enabled", flush=True)
            print(
                f"[BUS] Default Bus: {self.default_bus_id} ({self.route_name})",
                flush=True,
            )
            print(
                f"[SYNC] Multi-bus: Trips are created per bus_id from ESP32 requests",
                flush=True,
            )
            print(
                f"[STATS] Collections: temp_entries, busPassengerList, unmatchedPassengers, tripSessions, seasonTicketMembers",
                flush=True,
            )

            # Initialize route detector
            try:
                self.route_detector = RouteDetector(self.db)
                print("[OK] Route detector initialized", flush=True)
            except Exception as e:
                print(f"[WARN] Route detector initialization failed: {e}", flush=True)
                self.route_detector = None

            # ── Contractor similarity threshold ──────────────────────────────
            # Single source of truth — check_contractor_match() MUST use this
            # value instead of its own local constant (which caused a split where
            # 0.60 was documented but 0.70 was actually enforced, silently rejecting
            # valid staff faces that scored 0.60-0.70).
            # 0.60 is correct: same-camera same-person scores 0.70-0.85;
            # 0.60 gives a 0.10 safety margin without rejecting valid matches.
            self.contractor_similarity_threshold = 0.60

            # Load active trips from database
            try:
                active_sessions = self.trip_sessions.find({"status": "active"})
                for session in active_sessions:
                    bus_id = session.get("bus_id")
                    if bus_id:
                        self.load_current_trip(bus_id)
                print("[OK] Active trips loaded from database", flush=True)
            except Exception as e:
                print(f"[WARN] Failed to load active trips: {e}", flush=True)

        except Exception as e:
            print(f"[ERROR] Failed to connect to MongoDB: {e}", flush=True)
            raise

    def generate_trip_id(self, start_time=None, bus_id=None):
        """Generate unique trip ID for a specific bus"""
        if start_time is None:
            start_time = datetime.now()
        if bus_id is None:
            bus_id = self.default_bus_id
        date_str = start_time.strftime("%Y-%m-%d")
        time_str = start_time.strftime("%H:%M")
        return f"{bus_id}_{date_str}_{time_str}"

    def _parse_timestamp_safe(self, timestamp_str):
        """Safely parse timestamp, handling invalid/epoch timestamps from ESP32"""
        try:
            if not timestamp_str:
                print(f"[WARN] Empty timestamp, using server time", flush=True)
                return datetime.now()

            # Handle timezone format: replace +00:00 with Z for fromisoformat compatibility
            timestamp_str = (
                str(timestamp_str).replace("+00:00", "Z").replace("Z", "+00:00")
            )

            # Try parsing with fromisoformat
            parsed_time = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))

            # Check if timestamp is before 2020 (likely unsynced ESP32 time)
            if parsed_time.year < 2020:
                print(
                    f"[WARN] Invalid timestamp detected (ESP32 time not synced): {timestamp_str}, using server time",
                    flush=True,
                )
                return datetime.now()

            # Remove timezone info to store as naive datetime (MongoDB compatibility)
            if parsed_time.tzinfo is not None:
                parsed_time = parsed_time.replace(tzinfo=None)

            print(
                f"[OK] Parsed timestamp: {timestamp_str} -> {parsed_time}", flush=True
            )
            return parsed_time
        except Exception as e:
            print(
                f"[WARN] Error parsing timestamp '{timestamp_str}': {e}, using server time",
                flush=True,
            )
            return datetime.now()

    def load_current_trip(self, bus_id=None):
        """Load active trip from database for specific bus or create new one"""
        if bus_id is None:
            bus_id = self.default_bus_id
        try:
            # Find active OR ending trip for this bus.
            # "ending" sessions must be restored too so that EXIT logs arriving
            # after the schedule window closes can still be matched within the
            # 30-minute grace period after a server restart.
            active_trip = self.trip_sessions.find_one(
                {"bus_id": bus_id, "status": {"$in": ["active", "ending"]}}
            )

            if active_trip:
                # Check for stale trip (older than 12 hours)
                start_time = active_trip.get("start_time")
                if start_time and (datetime.utcnow() - start_time).total_seconds() > (
                    12 * 3600
                ):
                    print(
                        f"[WARN] Found stale active trip {active_trip['trip_id']} (Started: {start_time}). Closing it.",
                        flush=True,
                    )

                    # --- PROCESS STALE ENTRIES BEFORE CLOSING ---
                    trip_id = active_trip["trip_id"]

                    # Count passengers
                    passenger_count = self.final_passengers.count_documents(
                        {"trip_id": trip_id}
                    )

                    # Move remaining temp_entries to unmatched
                    remaining = list(
                        self.temp_entries.find({"trip_id": trip_id, "bus_id": bus_id})
                    )

                    print(
                        f"[CLEANUP] Processing {len(remaining)} stale temp_entries for {trip_id}",
                        flush=True,
                    )

                    unmatched_count = 0
                    for entry in remaining:
                        unmatched_entry = {
                            "trip_id": trip_id,
                            "bus_id": bus_id,
                            "route_name": entry.get("route_name", self.route_name),
                            "type": "ENTRY",
                            "trip_start_time": start_time,
                            "face_id": entry.get("face_id", 0),
                            "face_embedding": entry.get("face_embedding", []),
                            "embedding_size": entry.get("embedding_size", 0),
                            "location": entry.get("entry_location", {}),
                            "timestamp": entry.get("entry_timestamp"),
                            "best_similarity_found": 0.0,
                            "reason": "Auto-closed stale trip",
                            "created_at": datetime.now(),
                        }
                        self.unmatched_passengers.insert_one(unmatched_entry)
                        unmatched_count += 1

                    # Delete temp entries
                    self.temp_entries.delete_many({"trip_id": trip_id})

                    # Close the stale trip
                    self.trip_sessions.update_one(
                        {"_id": active_trip["_id"]},
                        {
                            "$set": {
                                "status": "completed_auto_cleanup",
                                "end_time": datetime.utcnow(),
                                "total_passengers": passenger_count,
                                "total_unmatched": unmatched_count,
                                "note": "Auto-closed by server restart (stale)",
                            }
                        },
                    )
                    # Start fresh trip
                    self.start_new_trip(bus_id=bus_id)
                else:
                    restored_status = active_trip.get("status", "active")
                    self.current_trips[bus_id] = {
                        "trip_id": active_trip["trip_id"],
                        "bus_id": bus_id,
                        "route_name": active_trip.get("route_name", self.route_name),
                        "start_time": active_trip["start_time"],
                        "status": restored_status,  # preserve "ending" after restart
                        "_id": active_trip["_id"],
                    }
                    print(
                        f"[LOC] Loaded {restored_status} trip for {bus_id}: {active_trip['trip_id']}",
                        flush=True,
                    )
            else:
                # Auto-start new trip for this bus
                self.start_new_trip(bus_id=bus_id)
        except Exception as e:
            print(f"[ERROR] Error loading trip for {bus_id}: {e}", flush=True)
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
            if (
                bus_id in self.current_trips
                and self.current_trips[bus_id].get("status") == "active"
            ):
                self.end_current_trip(bus_id=bus_id)

            # Cleanup orphaned entries for this bus
            self.cleanup_old_temp_entries(hours_old=0, bus_id=bus_id)

            # Generate trip ID
            trip_id = self.generate_trip_id(start_time, bus_id)

            # Smart route detection based on GPS
            detected_route = self.route_name  # Default
            if initial_gps and hasattr(self, "route_detector") and self.route_detector:
                route_info = self.route_detector.detect_route_direction(
                    bus_id, initial_gps, start_time
                )
                if route_info:
                    detected_route = route_info["route_name"]
                    print(
                        f"[ROUTE] Auto-detected route for {bus_id}: {detected_route}",
                        flush=True,
                    )

            # Create trip session record
            trip_session = {
                "trip_id": trip_id,
                "bus_id": bus_id,
                "route_name": detected_route,
                "start_time": start_time,
                "end_time": None,
                "status": "active",
                "total_passengers": 0,
                "total_unmatched": 0,
                "route_detection_gps": initial_gps,
                "created_at": datetime.utcnow(),
            }

            result = self.trip_sessions.insert_one(trip_session)

            # Store current trip info for this bus
            self.current_trips[bus_id] = {
                "trip_id": trip_id,
                "bus_id": bus_id,
                "route_name": detected_route,
                "start_time": start_time,
                "status": "active",
                "_id": result.inserted_id,
            }

            print(f"[BUS] Started new trip for {bus_id}: {trip_id}", flush=True)
            return trip_id
        except Exception as e:
            print(f"[ERROR] Error starting trip for {bus_id}: {e}", flush=True)
            return None

    def end_current_trip(self, bus_id=None):
        """Wrapper to end the currently tracked trip for a bus"""
        if bus_id is None:
            bus_id = self.default_bus_id

        current_trip = self.current_trips.get(bus_id)
        if not current_trip:
            print(f"[ERROR] No active trip in memory for {bus_id}", flush=True)
            return False

        return self.close_trip_session(bus_id, current_trip["trip_id"])

    def close_trip_session(self, bus_id, trip_id):
        """
        TRIP-WISE CLEANUP: Close a specific trip session and move its unmatched entries.
        This allows cleaning up old sessions even if they aren't 'current' in memory.
        """
        try:
            # Find the trip session in DB
            session = self.trip_sessions.find_one(
                {"trip_id": trip_id, "bus_id": bus_id}
            )
            if not session:
                print(f"[ERROR] Session {trip_id} not found in database", flush=True)
                return False

            print(
                f"[CLEANUP] Closing Session {trip_id} for bus {bus_id}...", flush=True
            )

            # 1. Count matched passengers for this SPECIFIC trip
            passenger_count = self.final_passengers.count_documents(
                {"trip_id": trip_id}
            )

            # 2. Find ONLY the temp_entries belonging to THIS trip_id
            remaining = list(
                self.temp_entries.find({"trip_id": trip_id, "bus_id": bus_id})
            )

            print(
                f"   -> Found {len(remaining)} unmatched ENTRY records for this session",
                flush=True,
            )

            unmatched_count = 0
            for entry in remaining:
                unmatched_entry = {
                    "trip_id": trip_id,
                    "bus_id": bus_id,
                    "route_name": entry.get("route_name", self.route_name),
                    "type": "ENTRY",
                    "trip_start_time": session.get("start_time"),
                    "face_id": entry.get("face_id", 0),
                    "face_embedding": entry.get("face_embedding", []),
                    "embedding_size": entry.get("embedding_size", 0),
                    "location": entry.get("entry_location", {}),
                    "timestamp": entry.get("entry_timestamp"),
                    "best_similarity_found": 0.0,
                    "reason": "Session ended automatically",
                    "created_at": datetime.now(),
                }
                self.unmatched_passengers.insert_one(unmatched_entry)
                unmatched_count += 1

            # 3. Delete ONLY the temp entries for THIS trip_id AND this bus_id.
            # NOTE: bus_id filter is REQUIRED — without it a rare trip_id collision
            # across two buses would silently delete entries from the wrong bus.
            deleted_count = self.temp_entries.delete_many(
                {"trip_id": trip_id, "bus_id": bus_id}
            ).deleted_count
            print(
                f"   -> Deleted {deleted_count} temp_entries for {trip_id} / {bus_id}",
                flush=True,
            )

            # 4. Update the Trip Session record
            self.trip_sessions.update_one(
                {"_id": session["_id"]},
                {
                    "$set": {
                        "status": "completed",
                        "end_time": datetime.utcnow(),
                        "total_passengers": passenger_count,
                        "total_unmatched": unmatched_count,
                        "closed_at": datetime.now(),
                    }
                },
            )

            # 5. If this was the 'current' trip in memory, remove it
            if (
                bus_id in self.current_trips
                and self.current_trips[bus_id]["trip_id"] == trip_id
            ):
                del self.current_trips[bus_id]

            print(
                f"[OK] Session {trip_id} finalized. Unmatched moved: {unmatched_count}",
                flush=True,
            )
            return True
        except Exception as e:
            print(f"[ERROR] Error closing session {trip_id}: {e}", flush=True)
            return False

    def mark_trip_ending(self, bus_id, trip_id):
        """
        Transition a trip from 'active' → 'ending' to start the 30-minute grace window.

        Trip state machine
        ──────────────────
          active  ──(schedule window ends)──►  ending  ──(TRIP_END_GRACE_MINUTES)──►  [close_trip_session]
                                                                                              ↓
                                                                              temp_entries → unmatchedPassengers
                                                                              status      → "completed"

        While 'ending':
          • EXIT logs are still accepted — passengers who boarded but haven't alighted
            yet can still be matched within the grace window.
          • ENTRY logs are REJECTED — the bus has arrived; no new boardings.
          • The in-memory current_trips entry is kept (status set to "ending") so that
            find_matching_entry() can still resolve the trip_id for EXIT matching.
        """
        try:
            session = self.trip_sessions.find_one(
                {"trip_id": trip_id, "bus_id": bus_id}
            )
            if not session:
                print(
                    f"[WARN] mark_trip_ending: Session {trip_id} not found in DB",
                    flush=True,
                )
                return False

            current_status = session.get("status")
            if current_status != "active":
                print(
                    f"[WARN] mark_trip_ending: Session {trip_id} is already "
                    f"'{current_status}' — nothing to do.",
                    flush=True,
                )
                return False

            now_utc = datetime.utcnow()
            grace_ends_at = now_utc + timedelta(minutes=TRIP_END_GRACE_MINUTES)

            self.trip_sessions.update_one(
                {"_id": session["_id"]},
                {
                    "$set": {
                        "status": "ending",
                        "end_detected_at": now_utc,
                        "grace_ends_at": grace_ends_at,
                    }
                },
            )

            # Mirror the status change in the in-memory cache so that
            # find_matching_entry() can still resolve this trip for EXIT logs.
            if (
                bus_id in self.current_trips
                and self.current_trips[bus_id]["trip_id"] == trip_id
            ):
                self.current_trips[bus_id]["status"] = "ending"

            print(
                f"[BG] Trip {trip_id} → ENDING. "
                f"Grace window: {TRIP_END_GRACE_MINUTES} min "
                f"(auto-close at {grace_ends_at.strftime('%H:%M:%S')} UTC).",
                flush=True,
            )
            return True

        except Exception as e:
            print(f"[ERROR] mark_trip_ending({trip_id}): {e}", flush=True)
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
                "trip_id": None,
                "bus_id": bus_id,
                "route_name": self.route_name,
                "status": "waiting_for_departure",
                "trip_active": False,
                "passengers_inside": 0,
                "passengers_completed": 0,
                "duration_minutes": 0,
            }

        try:
            trip_session = self.trip_sessions.find_one({"_id": current_trip["_id"]})

            if trip_session:
                passenger_count = self.final_passengers.count_documents(
                    {"trip_id": current_trip["trip_id"]}
                )
                temp_count = self.temp_entries.count_documents(
                    {"trip_id": current_trip["trip_id"]}
                )
                duration_minutes = (
                    datetime.now() - trip_session["start_time"]
                ).total_seconds() / 60

                # Simplified status
                if duration_minutes < 60:
                    trip_status = "departing"
                elif duration_minutes < 480:  # 8 hours
                    trip_status = "in_transit"
                else:
                    trip_status = "approaching_destination"

                return {
                    "trip_id": current_trip["trip_id"],
                    "bus_id": bus_id,
                    "route_name": trip_session.get("route_name", self.route_name),
                    "start_time": trip_session["start_time"].isoformat(),
                    "status": trip_status,
                    "trip_active": True,
                    "passengers_completed": passenger_count,
                    "passengers_inside": temp_count,
                    "duration_minutes": round(duration_minutes, 1),
                }
            return None
        except Exception as e:
            print(f"[ERROR] Error getting trip for {bus_id}: {e}", flush=True)
            return None

    def get_all_trips(self, limit=10, bus_id=None):
        """Get recent trips for specific bus or all buses"""
        try:
            query = {}
            if bus_id:
                query["bus_id"] = bus_id

            trips = list(
                self.trip_sessions.find(query).sort("start_time", -1).limit(limit)
            )

            return trips
        except Exception as e:
            print(f"[ERROR] Error getting trips: {e}", flush=True)
            return []

    def generate_passenger_id(self):
        """Generate unique passenger ID"""
        count = self.final_passengers.count_documents({}) + 1
        return f"PASS_{count:06d}"

    def calculate_road_distance_osrm(self, start_lat, start_lon, end_lat, end_lon):
        """Calculate road distance using OSRM API (free, no API key required)"""
        try:
            url = f"{self.distance_api_config['osrm_base_url']}/{start_lon},{start_lat};{end_lon},{end_lat}"
            params = {"overview": "false", "geometries": "geojson", "steps": "false"}

            response = requests.get(
                url, params=params, timeout=self.distance_api_config["timeout"]
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("code") == "Ok" and data.get("routes"):
                    # Distance in meters, convert to kilometers
                    distance_km = data["routes"][0]["distance"] / 1000
                    duration_seconds = data["routes"][0]["duration"]

                    return {
                        "distance_km": round(distance_km, 2),
                        "duration_minutes": round(duration_seconds / 60, 1),
                        "provider": "osrm",
                        "success": True,
                    }

            print(f"[ERROR] OSRM API error: {status_code} - {text}", flush=True)
            return None

        except Exception as e:
            print(f"[ERROR] Error with OSRM API: {e}", flush=True)
            return None

    # Removed: calculate_road_distance_openrouteservice() - Unused, OSRM is default

    def calculate_road_distance(self, start_lat, start_lon, end_lat, end_lon):
        """Calculate road distance using OSRM with Haversine fallback"""
        try:
            if not all([start_lat, start_lon, end_lat, end_lon]):
                return None

            start_lat, start_lon = float(start_lat), float(start_lon)
            end_lat, end_lon = float(end_lat), float(end_lon)

            if not (
                -90 <= start_lat <= 90
                and -180 <= start_lon <= 180
                and -90 <= end_lat <= 90
                and -180 <= end_lon <= 180
            ):
                return None

            # Try OSRM first
            result = self.calculate_road_distance_osrm(
                start_lat, start_lon, end_lat, end_lon
            )

            if not result:
                print(
                    f"[WARN] OSRM failed. No road distance available. Journey may be inaccurately priced.",
                    flush=True,
                )
                return {
                    "distance_km": 0.0,
                    "duration_minutes": 0.0,
                    "provider": "failed",
                    "success": False,
                    "note": "Road distance calculation failed",
                }

            return result

        except Exception as e:
            print(f"[ERROR] Error calculating road distance: {e}", flush=True)
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
            if not hasattr(self, "_location_cache"):
                self._location_cache = {}

            if cache_key in self._location_cache:
                return self._location_cache[cache_key]

            # Call Nominatim API
            url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=14&addressdetails=1"
            headers = {"User-Agent": "BusPassengerTracker/1.0"}

            response = requests.get(url, headers=headers, timeout=5)

            if response.status_code == 200:
                data = response.json()
                address = data.get("address", {})

                # Extract meaningful location name
                location_name = (
                    address.get("city")
                    or address.get("town")
                    or address.get("village")
                    or address.get("suburb")
                    or address.get("county")
                    or address.get("state")
                    or "Unknown Location"
                )

                # Cache the result
                self._location_cache[cache_key] = location_name

                # Rate limiting - wait 1 second between API calls
                time.sleep(1)

                return location_name
            else:
                print(
                    f"[WARN] Geocoding failed: HTTP {response.status_code}", flush=True
                )
                return None

        except Exception as e:
            print(f"[WARN] Reverse geocoding error: {e}", flush=True)
            return None

    def calculate_fare(self, distance_km):
        """Calculate fare based on distance using fareStages collection from MongoDB"""
        try:
            if not distance_km or distance_km <= 0:
                return 0.0

            # If distance is less than 100 meters (0.1 km), set price to 0
            if distance_km < 0.1:
                print(
                    f"[WARN] Distance too short ({distance_km} km < 100m), setting price to 0",
                    flush=True,
                )
                return 0.0

            # Calculate stage number (2 km per stage - official Sri Lankan bus fare system)
            STAGE_DISTANCE = 2.0
            stage_number = math.ceil(distance_km / STAGE_DISTANCE)

            # Fetch fare from fareStages collection
            fare_stage = self.db["fareStages"].find_one(
                {"stage_number": stage_number, "is_active": True}
            )

            if fare_stage:
                return float(fare_stage["fare"])

            # If exact stage not found, find the closest higher stage
            closest_stage = self.db["fareStages"].find_one(
                {"stage_number": {"$gte": stage_number}, "is_active": True},
                sort=[("stage_number", 1)],
            )

            if closest_stage:
                return float(closest_stage["fare"])

            # Fallback: use highest available stage
            highest_stage = self.db["fareStages"].find_one(
                {"is_active": True}, sort=[("stage_number", -1)]
            )

            if highest_stage:
                return float(highest_stage["fare"])

            # Final fallback: use old hardcoded calculation
            print(
                f"[WARN] No fare stages found in database, using fallback calculation",
                flush=True,
            )
            if stage_number == 1:
                return 30.0
            else:
                return 30.0 + ((stage_number - 1) * 10.0)

        except Exception as e:
            print(f"[ERROR] Error calculating fare: {e}", flush=True)
            return 0.0

    def check_season_ticket_member(
        self, face_embedding, bus_route=None, gps_location=None
    ):
        """
        Check if face matches season ticket members for THIS ROUTE ONLY

        Args:
            face_embedding: Face embedding to match
            bus_route: Current bus route (optional, for optimization)
            gps_location: Current GPS location (optional, for route detection)

        Returns:
            tuple: (member, similarity) or (None, 0.0)
        """
        try:
            if not face_embedding or len(face_embedding) == 0:
                print(
                    "[WARN] No face embedding provided for season ticket check",
                    flush=True,
                )
                return None, 0.0

            # Use UTC so the comparison is consistent with how valid_from / valid_until
            # are stored (datetime.utcnow() throughout the rest of the codebase).
            # Using datetime.now() (local Sri Lanka time = UTC+5:30) against UTC-stored
            # dates would cause valid tickets to appear expired 5.5 hours early.
            now = datetime.utcnow()

            # 2. Build query for active members.
            # is_active uses $ne False (not equal to False) so that member documents
            # inserted WITHOUT an is_active field are still included.
            # {"is_active": True} would silently exclude those legacy documents,
            # making valid season-ticket holders invisible to the face comparison.
            query = {
                "is_active": {"$ne": False},
                "valid_from": {"$lte": now},
                "valid_until": {"$gte": now},
                "face_embedding": {"$exists": True, "$ne": []},
            }

            # 3. OPTIMIZATION: Filter members by route if route_name is provided
            # This implements the user's request: "only check if bus has this waypoint"
            if bus_route:
                # We assume bus_route passed here is the route_name (e.g., "Jaffna-Colombo")
                print(f" Filtering season tickets for route: {bus_route}", flush=True)
                # Check if member's valid_routes patterns match this bus route
                # or if the bus route name contains member's from/to locations
                # We'll fetch all and filter in Python for complex pattern logic
                all_active_members = list(self.season_ticket_members.find(query))

                filtered_members = []
                for m in all_active_members:
                    is_relevant = False

                    # ── FIX: members with NO valid_routes defined are valid everywhere ──
                    # Previously, an empty / missing valid_routes list caused the inner
                    # loop to never execute, so is_relevant stayed False and the member
                    # was silently excluded from ALL face comparisons — they were never
                    # matched even when their face was a perfect cosine match.
                    member_routes = m.get("valid_routes")
                    if not member_routes:
                        # No route restriction defined → accept on every route
                        is_relevant = True
                    else:
                        for vr in member_routes:
                            patterns = vr.get("route_patterns", [])
                            from_l = vr.get("from_location", "").lower()
                            to_l = vr.get("to_location", "").lower()

                            # Case 1: Direct pattern match
                            if any(p.lower() in bus_route.lower() for p in patterns):
                                is_relevant = True
                                break

                            # Case 2: Bus route name mentions the locations
                            # (e.g. Jaffna-Colombo bus serves Jaffna-Kodikamam)
                            if (
                                from_l
                                and to_l
                                and from_l in bus_route.lower()
                                and to_l in bus_route.lower()
                            ):
                                is_relevant = True
                                break

                            # Case 3: No patterns defined in this entry - include
                            if not patterns:
                                is_relevant = True
                                break

                    if is_relevant:
                        filtered_members.append(m)

                all_active_members = filtered_members
                print(
                    f"[STATS] Filtered to {len(all_active_members)} relevant season ticket members for this route",
                    flush=True,
                )
            else:
                all_active_members = list(self.season_ticket_members.find(query))

            if not all_active_members:
                print(
                    "[WARN] No active season ticket members found in database",
                    flush=True,
                )
                return None, 0.0

            print(
                f"[SEARCH] Checking against {len(all_active_members)} active season ticket members",
                flush=True,
            )

            # NOTE: ESP32 embeddings from ESP-WHO are already L2-normalised by
            # transform_mfn_output(norm=true) inside enroll_id()/recognize().
            # Re-normalising here is a no-op for cosine similarity (sklearn
            # divides by norms internally) but creates a subtle inconsistency
            # versus find_matching_entry() which deliberately skips it.
            # Keep the array construction only.
            input_array = np.array(face_embedding, dtype=np.float32).reshape(1, -1)
            print(f"[STATS] Input embedding size: {input_array.shape}", flush=True)

            best_match = None
            best_similarity = 0.0
            all_similarities = []

            for member in all_active_members:
                if not member.get("face_embedding"):
                    continue

                member_name = member.get("name", "Unknown")
                member_id = member.get("member_id", "Unknown")

                # Convert member embedding to numpy array
                member_embedding = member["face_embedding"]

                # DIMENSIONALITY CHECK: Skip if dimensions don't match input
                if len(member_embedding) != input_array.shape[1]:
                    print(
                        f"   [SKIP] {member_name} ({member_id}): Dimension mismatch ({len(member_embedding)} vs {input_array.shape[1]})",
                        flush=True,
                    )
                    continue

                member_array = np.array(member_embedding, dtype=np.float32).reshape(
                    1, -1
                )
                # NOTE: Stored member embeddings are also ESP32-sourced and
                # already L2-normalised; sklearn cosine_similarity handles
                # normalisation internally, so no explicit re-normalisation needed.

                # Calculate cosine similarity
                try:
                    similarity = cosine_similarity(input_array, member_array)[0][0]
                    all_similarities.append((member_name, member_id, similarity))

                    print(
                        f"   - {member_name} ({member_id}): similarity = {similarity:.4f} (threshold: {self.season_ticket_similarity_threshold})",
                        flush=True,
                    )

                    # NOTE: AUTO-SYNC REMOVED — it was unsafe.
                    # The old logic updated a member's stored embedding with ANY incoming
                    # face that had a dimension mismatch (needs_hardware_sync=True OR
                    # embedding_size differs).  Because the check fires when similarity is
                    # BELOW the threshold (i.e. the face does NOT match), a complete
                    # stranger could have overwritten a legitimate member's profile and
                    # been granted a free ride at similarity=1.0.
                    # Use the dedicated /api/register-season-ticket endpoint to update
                    # member embeddings deliberately.

                    if similarity > best_similarity:
                        best_similarity = similarity
                        if similarity > self.season_ticket_similarity_threshold:
                            best_match = member
                except Exception as sim_err:
                    print(
                        f"   [ERR] Similarity calculation failed for {member_name}: {sim_err}",
                        flush=True,
                    )
                    continue

            # Print summary
            print(f"\n[OK] Season Ticket Check Summary:", flush=True)
            print(f"   Best similarity: {best_similarity:.4f}", flush=True)
            print(
                f"   Threshold: {self.season_ticket_similarity_threshold}", flush=True
            )
            print(
                f"   Match found: {'YES [OK]' if best_match else 'NO [ERROR]'}",
                flush=True,
            )

            if best_match:
                print(
                    f"[TICKET] Season ticket member detected: {best_match['name']} (similarity: {best_similarity:.3f})",
                    flush=True,
                )
                return best_match, best_similarity
            else:
                if best_similarity > 0:
                    print(
                        f"[WARN] Closest match was {best_similarity:.4f}, below threshold {self.season_ticket_similarity_threshold}",
                        flush=True,
                    )
                    print(
                        f" TIP: Consider lowering season_ticket_similarity_threshold if this is a valid member",
                        flush=True,
                    )

            return None, 0.0

        except Exception as e:
            print(f"[ERROR] Error checking season ticket: {e}", flush=True)
            import traceback

            traceback.print_exc()
            return None, 0.0

    def check_contractor_match(self, face_embedding, bus_id):
        """
        Check if face matches ANY contractor for THIS BUS

        Args:
            face_embedding: Face embedding to match
            bus_id: Current bus ID

        Returns:
            tuple: (contractor, similarity) or (None, 0.0)
        """
        # Use the instance-level threshold set in init_database() (0.60).
        # A local hard-coded 0.70 was previously used here, which silently
        # overrode the instance variable and rejected valid staff faces
        # scoring 0.60-0.70 (common in bus lighting conditions).
        #
        # SUSPECT threshold (0.40):
        # If the best similarity is in the range [0.40, threshold), the face is
        # "contractor-like" but below the confirmation bar.  This typically means
        # the contractor's stored embedding came from a different model (web-upload
        # float32 ONNX vs ESP32 INT8) or extreme lighting change.
        # Callers use the returned is_suspect flag to suppress storage so that a
        # contractor face is NEVER written to any collection even on a near-miss.
        CONTRACTOR_SUSPECT_THRESHOLD = 0.40
        try:
            if not face_embedding or len(face_embedding) == 0:
                return None, 0.0, False

            # Find ALL contractors for this specific bus (Driver, Conductor, etc.)
            # User Thesis Logic: Iterate through all staff
            # is_active filter: exclude only EXPLICITLY deactivated staff.
            # Using {"$ne": False} instead of True so that contractor documents
            # that were inserted WITHOUT an is_active field (the common case for
            # existing data) are still included.  {"is_active": True} silently
            # returns 0 results for those legacy docs, making the contractor check
            # always return None → their ENTRY gets stored → they get billed.
            contractors = list(
                self.contractors.find({"bus_id": bus_id, "is_active": {"$ne": False}})
            )

            if not contractors:
                print(
                    f"[WARN] check_contractor_match: 0 contractor records found for "
                    f"bus_id='{bus_id}'. Check that documents exist and bus_id matches "
                    f"exactly what the ESP32 sends.",
                    flush=True,
                )
                return None, 0.0, False

            # NOTE: ESP32 embeddings are already L2-normalised by transform_mfn_output().
            # Re-normalising is a no-op for cosine similarity (sklearn handles it
            # internally) and inconsistent with find_matching_entry() which deliberately
            # skips it.  Keep only the array construction.
            input_array = np.array(face_embedding, dtype=np.float32).reshape(1, -1)

            best_similarity = 0.0
            best_contractor = None

            print(
                f"[SEARCH] Checking against {len(contractors)} contractors for bus {bus_id}",
                flush=True,
            )

            for contractor in contractors:
                if not contractor.get("face_embedding"):
                    continue

                contractor_embedding = contractor["face_embedding"]

                # Dim Check
                if len(contractor_embedding) != len(face_embedding):
                    continue

                # NOTE: Stored contractor embeddings are ESP32-sourced and already
                # L2-normalised.  sklearn cosine_similarity normalises internally.
                contractor_array = np.array(
                    contractor_embedding, dtype=np.float32
                ).reshape(1, -1)

                sim = cosine_similarity(input_array, contractor_array)[0][0]
                print(f"   - Match vs {contractor.get('name')}: {sim:.4f}", flush=True)

                if sim > best_similarity:
                    best_similarity = sim
                    best_contractor = contractor

                # NOTE: AUTO-SYNC REMOVED — same security flaw as season ticket sync.
                # A weak match (0.40–0.70) is by definition NOT a confirmed identity;
                # overwriting the stored contractor embedding with an unverified face
                # and then returning similarity=1.0 means ANY stranger with a vaguely
                # similar face could be registered as a contractor and bypass fare checks.
                # Update contractor embeddings deliberately via the admin API instead.

            # ── Decision: CONFIRMED / SUSPECT / NO-MATCH ─────────────────────
            # CONFIRMED  similarity >= contractor_similarity_threshold (0.60)
            #            → face is a known contractor, ignore entirely
            # SUSPECT    similarity in [CONTRACTOR_SUSPECT_THRESHOLD, 0.60)
            #            → face is contractor-like (probably same person with
            #              model-mismatch or extreme lighting); suppress storage
            #              but do NOT create a journey record
            # NO-MATCH   similarity < CONTRACTOR_SUSPECT_THRESHOLD (0.40)
            #            → treat as a regular passenger
            if (
                best_contractor
                and best_similarity >= self.contractor_similarity_threshold
            ):
                print(
                    f"[OK] Contractor CONFIRMED: {best_contractor['name']} "
                    f"({best_similarity:.3f} >= {self.contractor_similarity_threshold})",
                    flush=True,
                )

                # Sync Check (Thesis Requirement)
                if best_contractor.get("needs_hardware_sync", False):
                    self.contractors.update_one(
                        {"_id": best_contractor["_id"]},
                        {
                            "$set": {
                                "needs_hardware_sync": False,
                                "last_synced": datetime.now(),
                            }
                        },
                    )

                return (
                    best_contractor,
                    best_similarity,
                    False,
                )  # is_suspect=False → confirmed

            if best_contractor and best_similarity >= CONTRACTOR_SUSPECT_THRESHOLD:
                # Similarity is in the grey zone [0.40, 0.60).
                # Most likely cause: contractor embedding was registered from the
                # web app using the float32 ONNX model while the ESP32 sends INT8
                # embeddings (different embedding spaces → lower cosine similarity
                # for the same person).  Suppress storage so the contractor is
                # never written to unmatchedPassengers or temp_entries.
                print(
                    f"[WARN] Contractor SUSPECT (not stored): {best_contractor['name']} "
                    f"sim={best_similarity:.3f} — in grey zone "
                    f"[{CONTRACTOR_SUSPECT_THRESHOLD}, {self.contractor_similarity_threshold}). "
                    f"Tip: re-register contractor embedding directly from ESP32 camera.",
                    flush=True,
                )
                return best_contractor, best_similarity, True  # is_suspect=True

            # Similarity < 0.40 → treat as a regular passenger
            if best_similarity > 0:
                print(
                    f"[INFO] Contractor check: best={best_similarity:.3f} "
                    f"< {CONTRACTOR_SUSPECT_THRESHOLD} (suspect floor) — treating as passenger.",
                    flush=True,
                )

            return None, best_similarity, False

        except Exception as e:
            print(f"[ERROR] Error checking contractor match: {e}", flush=True)
            return None, 0.0, False

    # Removed: _get_nearby_stops() - Never called
    # Removed: _get_location_name_variations() - Never called
    # Removed: _location_matches() - Never called

    def is_route_valid_for_season_ticket(self, member, entry_location, exit_location):
        """Check if journey is within season ticket valid routes using GPS-based detection"""
        try:
            if not member.get("valid_routes") or len(member["valid_routes"]) == 0:
                # No route restrictions - valid everywhere
                print("[OK] No route restrictions - valid everywhere", flush=True)
                return True, None

            entry_lat = entry_location.get("latitude")
            entry_lon = entry_location.get("longitude")
            exit_lat = exit_location.get("latitude")
            exit_lon = exit_location.get("longitude")

            # Validate GPS coordinates
            if not all([entry_lat, entry_lon, exit_lat, exit_lon]):
                print(
                    "[WARN] Missing GPS coordinates, falling back to route name matching",
                    flush=True,
                )
                # Fallback to old method if GPS not available
                return self._fallback_route_validation(member)

            # Use route detector if available
            if self.route_detector:
                print(f"[MAP] Using GPS-based route detection", flush=True)
                print(f"   Entry: {entry_lat}, {entry_lon}", flush=True)
                print(f"   Exit: {exit_lat}, {exit_lon}", flush=True)

                is_valid, match_info = (
                    self.route_detector.find_matching_season_ticket_routes(
                        entry_lat, entry_lon, exit_lat, exit_lon, member["valid_routes"]
                    )
                )

                if is_valid:
                    print(
                        f"[OK] GPS-based validation: Journey matches {match_info.get('matched_route')}",
                        flush=True,
                    )
                    return True, match_info
                else:
                    print(
                        f"[ERROR] GPS-based validation: {match_info.get('reason')}",
                        flush=True,
                    )
                    return False, match_info
            else:
                # Fallback if route detector not available
                print("[WARN] Route detector not available, using fallback", flush=True)
                return self._fallback_route_validation(member)

        except Exception as e:
            print(f"[ERROR] Error checking route validity: {e}", flush=True)
            import traceback

            traceback.print_exc()
            return False, None

    def _fallback_route_validation(self, member):
        """Fallback route validation using route name matching"""
        try:
            for valid_route in member["valid_routes"]:
                route_patterns = valid_route.get("route_patterns", [])

                if not route_patterns or len(route_patterns) == 0:
                    from_loc = valid_route.get("from_location", "").lower()
                    to_loc = valid_route.get("to_location", "").lower()

                    if (
                        from_loc in self.route_name.lower()
                        and to_loc in self.route_name.lower()
                    ):
                        print(
                            f"[OK] Fallback: Route {self.route_name} matches {from_loc}  {to_loc}",
                            flush=True,
                        )
                        return True, valid_route
                else:
                    for pattern in route_patterns:
                        if (
                            pattern.lower() in self.route_name.lower()
                            or self.route_name.lower() in pattern.lower()
                        ):
                            print(
                                f"[OK] Fallback: Route {self.route_name} matches pattern {pattern}",
                                flush=True,
                            )
                            return True, valid_route

            print(
                f"[ERROR] Fallback: Route {self.route_name} not in member's valid routes",
                flush=True,
            )
            return False, None

        except Exception as e:
            print(f"[ERROR] Error in fallback validation: {e}", flush=True)
            return False, None

    def store_entry(self, log_entry):
        """Store temporary entry for matching - supports multiple buses"""
        try:
            # Get bus_id from log entry, fallback to default
            bus_id = log_entry.get("bus_id", self.default_bus_id)

            # Ensure we have an active trip for this bus.
            # ENTRY logs are rejected during the "ending" grace window — the bus has
            # arrived and is no longer accepting new boardings.
            current_trip = self.get_current_trip_for_bus(bus_id)
            trip_status = current_trip.get("status") if current_trip else None

            if trip_status == "ending":
                print(
                    f"[REJECT] ENTRY rejected for {bus_id} — trip "
                    f"'{current_trip['trip_id']}' is in the 'ending' grace window. "
                    f"No new boardings accepted.",
                    flush=True,
                )
                return None

            if trip_status != "active":
                # No active trip found — auto-start one (e.g. first boot, or after a
                # completed trip with no schedule manager to create the next one).
                print(
                    f"[WARN] No active trip for {bus_id} (status={trip_status!r}), "
                    f"starting new trip...",
                    flush=True,
                )
                self.start_new_trip(bus_id=bus_id)
                current_trip = self.current_trips.get(bus_id)
                if not current_trip:
                    print(
                        f"[ERROR] Failed to start new trip for {bus_id}",
                        flush=True,
                    )
                    return None

            # Check if this is a season ticket member at entry
            # OPTIMIZED: Pass route and GPS for targeted checking
            gps_location = {
                "latitude": log_entry.get("latitude", 0),
                "longitude": log_entry.get("longitude", 0),
            }
            season_member, season_similarity = self.check_season_ticket_member(
                log_entry.get("face_embedding", []),
                bus_route=current_trip.get(
                    "route_name"
                ),  # Use route_name for filtering (was trip_id)
                gps_location=gps_location,
            )

            season_ticket_detected = None
            if season_member:
                season_ticket_detected = {
                    "member_id": season_member["member_id"],
                    "member_name": season_member["name"],
                    "similarity_score": float(season_similarity),
                }
                print(
                    f"[TICKET] Season ticket member detected at ENTRY on {bus_id}: {season_member['name']}",
                    flush=True,
                )

            # Get location name
            location_name = self.reverse_geocode(
                log_entry.get("latitude", 0), log_entry.get("longitude", 0)
            )

            temp_entry = {
                "trip_id": current_trip["trip_id"],
                "trip_start_time": current_trip["start_time"],
                "bus_id": bus_id,  # Use bus_id from request
                "route_name": self.route_name,
                "face_id": log_entry.get("face_id", 0),
                "face_embedding": log_entry.get("face_embedding", []),
                "embedding_size": log_entry.get("embedding_size", 0),
                "season_ticket_detected": season_ticket_detected,
                "entry_location": {
                    "latitude": log_entry.get("latitude", 0),
                    "longitude": log_entry.get("longitude", 0),
                    "device_id": log_entry.get("device_id"),
                    "timestamp": log_entry.get("timestamp"),
                    "location_name": location_name,
                },
                "entry_timestamp": self._parse_timestamp_safe(
                    log_entry.get("timestamp")
                ),
                "created_at": datetime.utcnow(),
            }

            result = self.temp_entries.insert_one(temp_entry)
            print(
                f"[OK] Stored entry for {bus_id}: {result.inserted_id} (Trip: {current_trip['trip_id']})",
                flush=True,
            )
            return str(result.inserted_id)

        except Exception as e:
            print(f"[ERROR] Error storing entry: {e}", flush=True)
            import traceback

            traceback.print_exc()
            return None

    def find_matching_entry(self, exit_log):
        """Find matching entry and create final passenger record - supports multiple buses"""
        if not exit_log.get("face_embedding"):
            return None, 0.0

        try:
            # Get bus_id from exit log, fallback to default
            bus_id = exit_log.get("bus_id", self.default_bus_id)

            # Ensure we have an active OR ending trip for this bus.
            # EXIT logs must still be matched during the 30-minute "ending" grace
            # window so passengers who boarded but haven't alighted yet are not
            # wrongly moved to unmatchedPassengers.
            current_trip = self.get_current_trip_for_bus(bus_id)
            trip_status = current_trip.get("status") if current_trip else None
            if not current_trip or trip_status not in ("active", "ending"):
                print(
                    f"[WARN] No active/ending trip for {bus_id} for exit matching "
                    f"(status={trip_status!r})",
                    flush=True,
                )
                return None, 0.0

            # Calculate time threshold for matching window
            time_threshold = datetime.now() - timedelta(hours=self.time_window_hours)

            # Build query - MUST filter by bus_id to only match entries from this bus
            query = {
                "trip_id": current_trip["trip_id"],  # Only match within same trip
                "bus_id": bus_id,  # CRITICAL: Only match entries from this bus!
                "entry_timestamp": {"$gte": time_threshold},
                "face_embedding": {"$exists": True, "$ne": []},
            }

            # DEBUG: Print query to verify bus_id filtering
            print(
                f" Query for matching: bus_id={bus_id}, trip_id={current_trip['trip_id']}",
                flush=True,
            )

            unmatched_entries = self.temp_entries.find(query).sort(
                "entry_timestamp", -1
            )

            entries_list = list(unmatched_entries)
            print(
                f"[SEARCH] Found {len(entries_list)} entries for {bus_id} (Trip: {current_trip['trip_id']})",
                flush=True,
            )

            # DEBUG: Show which bus_ids are in the results
            if entries_list:
                found_bus_ids = set(e.get("bus_id", "UNKNOWN") for e in entries_list)
                print(f"   Bus IDs in results: {found_bus_ids}", flush=True)

            if not entries_list:
                print(
                    f"[ERROR] No unmatched entries found for bus {bus_id}", flush=True
                )
                print(f"   Time threshold: {time_threshold}", flush=True)
                return None, 0.0

            print(
                f"[SEARCH] Checking {len(entries_list)} entries for similarity",
                flush=True,
            )

            # Convert exit embedding to numpy array
            # NOTE: ESP32 embeddings from ESP-WHO are already L2-normalized
            # Do NOT re-normalize to avoid corrupting the embedding direction
            exit_array = np.array(exit_log["face_embedding"], dtype=np.float32).reshape(
                1, -1
            )

            best_match = None
            best_similarity = 0.0
            actual_best_similarity = 0.0  # Track actual best even if below threshold

            for entry in entries_list:
                if not entry.get("face_embedding"):
                    continue

                entry_embedding = entry["face_embedding"]

                # ── Embedding validation (replaces bare dimension check) ──────
                # validate_esp32_embedding checks: correct 128-dim, no NaN/Inf,
                # non-zero norm.  This catches:
                #   • Old database entries with wrong dimension (e.g. Dlib 128 vs
                #     MobileFaceNet 128 — same size but different space — are
                #     caught by the norm/NaN guards if corrupted).
                #   • Flash-corruption artefacts (all-zero or NaN embeddings)
                #     that would produce a misleadingly high cosine similarity of
                #     exactly 0.0 instead of failing gracefully.
                emb_valid, emb_reason = validate_esp32_embedding(
                    entry_embedding,
                    context=f"stored_entry/{entry['_id']}",
                )
                if not emb_valid:
                    print(
                        f"  Entry {entry['_id']} [SKIP]: Invalid stored embedding — {emb_reason}",
                        flush=True,
                    )
                    continue

                # Dimension must also match the exit embedding's dimension
                if len(entry_embedding) != exit_array.shape[1]:
                    print(
                        f"  Entry {entry['_id']} [SKIP]: Dimension mismatch ({len(entry_embedding)} vs {exit_array.shape[1]})",
                        flush=True,
                    )
                    continue

                # Convert entry embedding to numpy array
                # NOTE: ESP32 embeddings from ESP-WHO are already L2-normalized
                # Do NOT re-normalize to avoid corrupting the embedding direction
                entry_array = np.array(entry_embedding, dtype=np.float32).reshape(1, -1)

                # Calculate cosine similarity
                try:
                    similarity = cosine_similarity(exit_array, entry_array)[0][0]

                    if similarity > actual_best_similarity:
                        actual_best_similarity = similarity

                    print(
                        f"  Entry {entry['_id']}: similarity = {similarity:.3f} (threshold: {self.similarity_threshold})",
                        flush=True,
                    )

                    if (
                        similarity > best_similarity
                        and similarity > self.similarity_threshold
                    ):
                        best_similarity = similarity
                        best_match = entry
                except Exception as sim_err:
                    print(
                        f"  Entry {entry['_id']} [ERR]: Similarity calculation failed: {sim_err}",
                        flush=True,
                    )
                    continue

            print(
                f" Best match: similarity = {actual_best_similarity:.3f} (Passed threshold: {'YES' if best_match else 'NO'})",
                flush=True,
            )

            if best_match:
                # Create final passenger record
                passenger_id = self.generate_passenger_id()

                # Calculate road distance between entry and exit points
                distance_info = self.calculate_road_distance(
                    best_match["entry_location"]["latitude"],
                    best_match["entry_location"]["longitude"],
                    exit_log.get("latitude", 0),
                    exit_log.get("longitude", 0),
                )

                # Check if passenger is a season ticket member
                # OPTIMIZED: Pass route and GPS for targeted checking
                exit_gps_location = {
                    "latitude": exit_log.get("latitude", 0),
                    "longitude": exit_log.get("longitude", 0),
                }
                season_member, season_similarity = self.check_season_ticket_member(
                    exit_log["face_embedding"],
                    bus_route=current_trip.get(
                        "route_name"
                    ),  # Pass route_name for filtering
                    gps_location=exit_gps_location,
                )

                is_season_ticket = False
                season_ticket_info = None
                price = 0.0

                if season_member:
                    # Check if route is valid for this season ticket
                    exit_location = {
                        "latitude": exit_log.get("latitude", 0),
                        "longitude": exit_log.get("longitude", 0),
                    }
                    is_route_valid, valid_route = self.is_route_valid_for_season_ticket(
                        season_member, best_match["entry_location"], exit_location
                    )

                    if is_route_valid:
                        # Season ticket is valid for this route - no charge
                        is_season_ticket = True
                        price = 0.0
                        season_ticket_info = {
                            "member_id": season_member["member_id"],
                            "member_name": season_member["name"],
                            "ticket_type": season_member.get("ticket_type", "monthly"),
                            "valid_until": season_member["valid_until"].isoformat()
                            if season_member.get("valid_until")
                            else None,
                            "similarity_score": float(season_similarity),
                            "valid_route": valid_route,
                        }
                        print(
                            f"[TICKET] Season ticket applied: {season_member['name']} - Price: 0",
                            flush=True,
                        )

                        # Update member statistics
                        self.season_ticket_members.update_one(
                            {"_id": season_member["_id"]},
                            {
                                "$inc": {"total_trips": 1},
                                "$set": {"last_used": datetime.now()},
                            },
                        )
                    else:
                        # Season ticket not valid for this route - calculate normal price
                        distance_km = (
                            distance_info.get("distance_km", 0) if distance_info else 0
                        )
                        price = self.calculate_fare(distance_km)
                        print(
                            f"[WARN] Season ticket not valid for route {self.route_name} - Charging normal price: {price}",
                            flush=True,
                        )
                else:
                    # Regular passenger - calculate normal price
                    distance_km = (
                        distance_info.get("distance_km", 0) if distance_info else 0
                    )
                    price = self.calculate_fare(distance_km)

                stage_number = (
                    math.ceil(distance_info.get("distance_km", 0) / 2.0)
                    if distance_info and distance_info.get("distance_km", 0) > 0
                    else 0
                )

                # Reverse geocode locations to get place names
                entry_location_name = self.reverse_geocode(
                    best_match["entry_location"]["latitude"],
                    best_match["entry_location"]["longitude"],
                )
                exit_location_name = self.reverse_geocode(
                    exit_log.get("latitude", 0), exit_log.get("longitude", 0)
                )

                final_passenger = {
                    "id": passenger_id,
                    "trip_id": current_trip["trip_id"],
                    "trip_start_time": current_trip["start_time"],
                    "bus_id": bus_id,  # Use bus_id from request
                    "route_name": self.route_name,
                    "is_season_ticket": is_season_ticket,
                    "season_ticket_info": season_ticket_info,
                    "entryLocation": {
                        "latitude": best_match["entry_location"]["latitude"],
                        "longitude": best_match["entry_location"]["longitude"],
                        "device_id": best_match["entry_location"]["device_id"],
                        "timestamp": best_match["entry_location"]["timestamp"],
                        "location_name": entry_location_name,  # NEW!
                    },
                    "exitLocation": {
                        "latitude": exit_log.get("latitude", 0),
                        "longitude": exit_log.get("longitude", 0),
                        "device_id": exit_log.get("device_id"),
                        "timestamp": exit_log.get("timestamp"),
                        "location_name": exit_location_name,  # NEW!
                    },
                    "entry_timestamp": best_match["entry_timestamp"],
                    "exit_timestamp": self._parse_timestamp_safe(
                        exit_log.get("timestamp")
                    ),
                    "journey_duration_minutes": (
                        self._parse_timestamp_safe(exit_log.get("timestamp"))
                        - best_match["entry_timestamp"]
                    ).total_seconds()
                    / 60,
                    "similarity_score": float(best_similarity),
                    "entry_face_id": best_match.get("face_id", 0),
                    "exit_face_id": exit_log.get("face_id", 0),
                    "distance_info": distance_info
                    if distance_info
                    else {
                        "distance_km": 0.0,
                        "duration_minutes": 0.0,
                        "provider": "unavailable",
                        "success": False,
                        "note": "Distance calculation failed",
                    },
                    "price": price,
                    "stage_number": stage_number,
                    "created_at": datetime.now(),
                }

                # Insert final passenger record
                result = self.final_passengers.insert_one(final_passenger)

                # Delete the temporary entry immediately
                self.temp_entries.delete_one({"_id": best_match["_id"]})

                print(f"[OK] Created final passenger: {passenger_id}", flush=True)
                if distance_info and distance_info.get("success"):
                    print(
                        f" Journey distance: {distance_info['distance_km']} km (estimated {distance_info['duration_minutes']} min)",
                        flush=True,
                    )
                print(f"[DEL] Deleted temporary entry: {best_match['_id']}", flush=True)

                return final_passenger, best_similarity

            return None, actual_best_similarity

        except Exception as e:
            print(f"[ERROR] Error finding matching entry: {e}", flush=True)
            return None, 0.0

    def process_face_log(self, log_entry):
        """Process incoming face log entry - supports multiple buses"""
        location_type = log_entry.get("location_type", "").upper()
        bus_id = log_entry.get("bus_id", self.default_bus_id)

        # 1. Extraction from IMAGE if provided AND embedding is missing
        # ── MODEL-MISMATCH WARNING ────────────────────────────────────────────
        # The ESP32 uses the INT8-quantised MobileFaceNet (FaceRecognition112V1S8).
        # The server's face_recognition_helper uses a float32 MobileFaceNet ONNX.
        # These two models produce embeddings in slightly different spaces even for
        # the same face — comparing an ESP32 embedding (entry) against a server-
        # extracted embedding (exit, or vice-versa) WILL cause false non-matches.
        # Fallback extraction is therefore only used as a last resort and the log
        # is flagged so operators know the embedding may not cross-compare cleanly.
        # ─────────────────────────────────────────────────────────────────────
        image_data = log_entry.get("image_data") or log_entry.get("image")
        if image_data and not log_entry.get("face_embedding"):
            print(
                f"[WARN] Missing ESP32 embedding — falling back to server-side ONNX extraction.",
                flush=True,
            )
            print(
                f"[WARN] ⚠️  Server ONNX model (float32) != ESP32 S8 model (INT8). "
                f"Cross-camera cosine similarity may be unreliable for this log entry.",
                flush=True,
            )
            try:
                from face_recognition_helper import extract_face_embedding_from_base64

                extract_res = extract_face_embedding_from_base64(
                    image_data, draw_boxes=False
                )
                if extract_res.get("success") and extract_res.get("face_embedding"):
                    log_entry["face_embedding"] = extract_res["face_embedding"]
                    log_entry["embedding_source"] = "server_onnx_fallback"
                    print(
                        f"[WARN] Fallback embedding extracted "
                        f"({'MOCK' if extract_res.get('is_mock') else 'REAL ONNX'}) — "
                        f"cross-camera match may fail.",
                        flush=True,
                    )
            except Exception as e:
                print(
                    f"[WARN] Failed to extract fallback embedding from image_data: {e}",
                    flush=True,
                )

        face_embedding = log_entry.get("face_embedding", [])

        # 2. Validate the embedding before any further processing
        emb_valid, emb_reason = validate_esp32_embedding(
            face_embedding,
            context=f"{location_type}/{bus_id}/face_id={log_entry.get('face_id', '?')}",
        )
        if not emb_valid:
            print(
                f"[ERROR] Rejecting log entry — invalid embedding: {emb_reason}",
                flush=True,
            )
            return {
                "action": "rejected_invalid_embedding",
                "bus_id": bus_id,
                "face_id": log_entry.get("face_id", 0),
                "message": f"Invalid embedding rejected: {emb_reason}",
            }

        if location_type == "ENTRY":
            # ── ENTRY processing ──────────────────────────────────────────────
            # Priority 1: Contractor / staff face  → IGNORE entirely (nothing stored)
            # Priority 2: Regular passenger        → store in temp_entries
            #
            # check_contractor_match returns (contractor, similarity, is_suspect):
            #   contractor != None AND is_suspect=False  → confirmed staff
            #   contractor != None AND is_suspect=True   → grey-zone / model-mismatch
            #   contractor == None                       → regular passenger
            # Both confirmed AND suspect contractor faces are suppressed at ENTRY.
            contractor, contractor_sim, contractor_suspect = (
                self.check_contractor_match(face_embedding, bus_id)
            )
            if contractor:
                label = "SUSPECT" if contractor_suspect else "CONFIRMED"
                print(
                    f"[STAFF] Contractor {label} at ENTRY: {contractor['name']} "
                    f"on {bus_id} (sim={contractor_sim:.3f}). Not stored.",
                    flush=True,
                )
                return {
                    "action": "ignored_contractor",
                    "contractor_name": contractor["name"],
                    "contractor_label": label,
                    "bus_id": bus_id,
                    "face_id": log_entry.get("face_id", 0),
                    "similarity": float(contractor_sim),
                    "is_suspect": contractor_suspect,
                    "message": (
                        f"[STAFF] Contractor {label}: {contractor['name']} — "
                        f"not stored (sim={contractor_sim:.3f})."
                    ),
                }

            # Store temporary entry
            entry_id = self.store_entry(log_entry)

            if entry_id:
                return {
                    "action": "stored_entry",
                    "entry_id": entry_id,
                    "bus_id": bus_id,
                    "face_id": log_entry.get("face_id", 0),
                    "message": f"Entry stored for {bus_id} (face_id: {log_entry.get('face_id', 0)})",
                }
            else:
                return {
                    "action": "error",
                    "bus_id": bus_id,
                    "message": "Failed to store entry",
                }

        elif location_type == "EXIT":
            # ── EXIT processing ───────────────────────────────────────────────
            # Exactly symmetric with ENTRY — contractor faces are NEVER stored
            # in any collection regardless of which camera sees them.
            #
            # Priority 1 (CONFIRMED contractor)  → stale cleanup + IGNORE
            # Priority 1 (SUSPECT contractor)    → stale cleanup + IGNORE
            # Priority 2 (regular passenger, entry match found) → JOURNEY RECORD
            # Priority 3 (regular passenger, no entry match)    → UNMATCHED EXIT
            #
            # "SUSPECT" covers the case where the stored contractor embedding was
            # registered from the web app (float32 ONNX) while the ESP32 sends
            # INT8 embeddings — different embedding spaces reduce cosine similarity
            # for the same person.  Suppressing storage prevents the contractor's
            # face from ever appearing in unmatchedPassengers.

            contractor, contractor_sim, contractor_suspect = (
                self.check_contractor_match(face_embedding, bus_id)
            )

            if contractor:
                # ── confirmed OR suspect contractor → ignore, nothing stored ──
                label = "SUSPECT" if contractor_suspect else "CONFIRMED"
                print(
                    f"[STAFF] Contractor {label} at EXIT: {contractor['name']} "
                    f"on {bus_id} (sim={contractor_sim:.3f}). Not stored.",
                    flush=True,
                )

                # ── Stale temp_entry cleanup ──────────────────────────────────
                # Under normal flow the ENTRY contractor check prevents any
                # temp_entry from being created.  But if a previous server
                # version (or a DB error) let one through, it would be swept
                # into unmatchedPassengers at trip-end.  Delete it now.
                # We use the SUSPECT threshold (0.40) as the match bar here so
                # that even model-mismatch entries are caught.
                STALE_CLEANUP_THRESHOLD = 0.40
                try:
                    current_trip = self.get_current_trip_for_bus(bus_id)
                    if current_trip:
                        stale_entries = list(
                            self.temp_entries.find(
                                {
                                    "bus_id": bus_id,
                                    "trip_id": current_trip["trip_id"],
                                    "face_embedding": {"$exists": True, "$ne": []},
                                }
                            )
                        )
                        exit_arr = np.array(face_embedding, dtype=np.float32).reshape(
                            1, -1
                        )
                        deleted_stale = 0
                        for stale in stale_entries:
                            try:
                                stale_arr = np.array(
                                    stale["face_embedding"], dtype=np.float32
                                ).reshape(1, -1)
                                if stale_arr.shape[1] != exit_arr.shape[1]:
                                    continue
                                sim = cosine_similarity(exit_arr, stale_arr)[0][0]
                                if sim >= STALE_CLEANUP_THRESHOLD:
                                    self.temp_entries.delete_one({"_id": stale["_id"]})
                                    deleted_stale += 1
                                    print(
                                        f"[DEL] Removed stale contractor temp_entry "
                                        f"{stale['_id']} (sim={sim:.3f})",
                                        flush=True,
                                    )
                            except Exception:
                                continue
                        if deleted_stale:
                            print(
                                f"[OK] Cleaned up {deleted_stale} stale contractor "
                                f"temp_entr{'y' if deleted_stale == 1 else 'ies'} "
                                f"for {contractor['name']} ({label})",
                                flush=True,
                            )
                except Exception as cleanup_err:
                    print(
                        f"[WARN] Stale temp_entry cleanup failed (non-fatal): "
                        f"{cleanup_err}",
                        flush=True,
                    )
                # ─────────────────────────────────────────────────────────────

                return {
                    "action": "ignored_contractor",
                    "contractor_name": contractor["name"],
                    "contractor_label": label,
                    "bus_id": bus_id,
                    "face_id": log_entry.get("face_id", 0),
                    "similarity": float(contractor_sim),
                    "is_suspect": contractor_suspect,
                    "message": (
                        f"[STAFF] Contractor {label} at EXIT: {contractor['name']} — "
                        f"not stored (sim={contractor_sim:.3f})."
                    ),
                }

            # ── Contractor check returned None → treat as regular passenger ──
            # Only reaches here when similarity < 0.40 (not contractor-like at all).

            # Priority 2: match against a stored entry record
            match_result, similarity = self.find_matching_entry(log_entry)

            if match_result:
                return {
                    "action": "matched_journey",
                    "passenger_id": match_result["id"],
                    "bus_id": bus_id,
                    "entry_face_id": match_result["entry_face_id"],
                    "exit_face_id": match_result["exit_face_id"],
                    "similarity": float(similarity),
                    "journey_duration": match_result["journey_duration_minutes"],
                    "message": (
                        f"[OK] Journey on {bus_id}! "
                        f"Passenger {match_result['id']} "
                        f"(similarity: {similarity:.3f}, "
                        f"duration: {match_result['journey_duration_minutes']:.1f} min)"
                    ),
                }

            # Priority 3: genuine unmatched passenger (not a contractor, no entry record)
            unmatched_exit_id = self.store_unmatched_exit(log_entry, similarity)
            return {
                "action": "unmatched_exit",
                "bus_id": bus_id,
                "face_id": log_entry.get("face_id", 0),
                "best_similarity": float(similarity),
                "unmatched_id": unmatched_exit_id,
                "message": (
                    f"[WARN] No entry match on {bus_id} for "
                    f"face_id {log_entry.get('face_id', 0)} "
                    f"(best similarity: {similarity:.3f})"
                ),
            }

        else:
            return {
                "action": "error",
                "bus_id": bus_id,
                "message": f"Unknown location_type: {location_type}",
            }

    def store_unmatched_exit(self, exit_log, best_similarity):
        """Store unmatched exit passenger - supports multiple buses"""
        try:
            # Get bus_id from exit log
            bus_id = exit_log.get("bus_id", self.default_bus_id)

            # Get current trip for this bus
            current_trip = self.get_current_trip_for_bus(bus_id)

            # Determine trip details (with fallback)
            if current_trip and current_trip.get("status") == "active":
                trip_id = current_trip["trip_id"]
                trip_start_time = current_trip["start_time"]
            else:
                print(
                    f"[WARN] No active trip for {bus_id} - storing unmatched exit with FALLBACK trip",
                    flush=True,
                )
                trip_id = f"FALLBACK_{bus_id}_{datetime.now().strftime('%Y%m%d_%H%M')}"
                trip_start_time = datetime.utcnow()

            # Get location name
            location_name = self.reverse_geocode(
                exit_log.get("latitude", 0), exit_log.get("longitude", 0)
            )

            unmatched_exit = {
                "trip_id": trip_id,
                "trip_start_time": trip_start_time,
                "bus_id": bus_id,  # Use bus_id from request
                "route_name": self.route_name,
                "type": "EXIT",
                "face_id": exit_log.get("face_id", 0),
                "face_embedding": exit_log.get("face_embedding", []),
                "embedding_size": exit_log.get("embedding_size", 0),
                "location": {
                    "latitude": exit_log.get("latitude", 0),
                    "longitude": exit_log.get("longitude", 0),
                    "device_id": exit_log.get("device_id"),
                    "timestamp": exit_log.get("timestamp"),
                    "location_name": location_name,
                },
                "timestamp": self._parse_timestamp_safe(exit_log.get("timestamp")),
                "best_similarity_found": float(best_similarity),
                "reason": "No matching entry found",
                "created_at": datetime.utcnow(),
            }

            result = self.unmatched_passengers.insert_one(unmatched_exit)
            print(
                f"[LOG] Stored unmatched exit for {bus_id}: {result.inserted_id}",
                flush=True,
            )
            return str(result.inserted_id)

        except Exception as e:
            print(f"[ERROR] Error storing unmatched exit: {e}", flush=True)
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
                print(
                    f"[SEARCH] Found {len(entries_to_clean)} temp entries to clean"
                    + (f" for {bus_id}" if bus_id else ""),
                    flush=True,
                )

                for entry in entries_to_clean:
                    entry_bus_id = entry.get("bus_id", self.default_bus_id)
                    unmatched_entry = {
                        "trip_id": entry.get("trip_id", "UNKNOWN"),
                        "bus_id": entry_bus_id,
                        "route_name": entry.get("route_name", self.route_name),
                        "type": "ENTRY",
                        "trip_start_time": entry.get("trip_start_time"),
                        "face_id": entry.get("face_id", 0),
                        "face_embedding": entry.get("face_embedding", []),
                        "embedding_size": entry.get("embedding_size", 0),
                        "location": entry.get("entry_location", {}),
                        "timestamp": entry.get("entry_timestamp"),
                        "best_similarity_found": 0.0,
                        "reason": reason,
                        "created_at": datetime.now(),
                    }
                    self.unmatched_passengers.insert_one(unmatched_entry)
                    print(
                        f"   -> Moved ENTRY face_id={entry.get('face_id')} to unmatchedPassengers",
                        flush=True,
                    )

                result = self.temp_entries.delete_many(query)

                print(
                    f"[DEL] Cleaned up {result.deleted_count} temp entries"
                    + (f" for {bus_id}" if bus_id else ""),
                    flush=True,
                )
                return len(entries_to_clean)
            else:
                print(
                    f"[OK] No temp entries to clean"
                    + (f" for {bus_id}" if bus_id else ""),
                    flush=True,
                )

            return 0

        except Exception as e:
            print(f"[ERROR] Error during cleanup: {e}", flush=True)
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
            print(f"[ERROR] Error parsing trip schedule: {e}", flush=True)
            return False

    # Removed: get_stats() - Never called, stats handled by Node.js backend


# Global tracker instance
bus_tracker = SimplifiedBusTracker()

# Add dynamic schedule manager (replaces old trip_scheduler)
schedule_manager = DynamicScheduleManager()

# Connect scheduler events to tracker cleanup logic
schedule_manager.on_trip_start = lambda bus, tid, dir: bus_tracker.start_new_trip(
    bus_id=bus
)
schedule_manager.on_trip_end = bus_tracker.end_current_trip

schedule_manager.start_scheduler_thread()

print(
    "[OK] Using DynamicScheduleManager for automated trip scheduling & cleanup",
    flush=True,
)


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
                "boards": [],
            }
            bus_tracker.power_configs.insert_one(default_config)
            config = default_config

        # Remove MongoDB _id
        if "_id" in config:
            del config["_id"]

        # ENRICH WITH DYNAMIC SCHEDULE (LIVE FETCH)
        if schedule_manager:
            try:
                # Always fetch the latest schedule for THIS specific bus from MongoDB
                trip_windows = schedule_manager.get_todays_trip_windows(bus_id=bus_id)
                if trip_windows:
                    print(
                        f"[OK] Injecting {len(trip_windows)} LIVE trip windows for {bus_id}",
                        flush=True,
                    )
                    config["trip_windows"] = trip_windows
                    config["use_multi_trip"] = True
                else:
                    print(
                        f" No active schedule windows found in MongoDB for {bus_id}",
                        flush=True,
                    )
            except Exception as e:
                print(
                    f"[WARN] Error injecting dynamic schedule for {bus_id}: {e}",
                    flush=True,
                )

        # Inject current server time for ESP32 sync
        config["current_server_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        return config
    except Exception as e:
        print(f"[ERROR] Error getting power config: {e}", flush=True)
        return None


class SimplifiedHandler(BaseHTTPRequestHandler):
    def _send_json_response(self, data, status_code=200):
        """Helper method to send JSON response with CORS headers"""
        response_data = json.dumps(data, default=str).encode()
        self.send_response(status_code)
        self.send_header("Content-type", "application/json")
        self.send_header("Content-Length", str(len(response_data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(response_data)

    def _send_error_response(self, message, status_code=500):
        """Helper method to send error response"""
        self._send_json_response(
            {"status": "error", "message": str(message)}, status_code
        )

    def _get_query_params(self):
        """Helper method to parse query parameters"""
        from urllib.parse import parse_qs, urlparse

        parsed_url = urlparse(self.path)
        return parse_qs(parsed_url.query)

    def do_OPTIONS(self):
        """Handle OPTIONS requests for CORS"""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        """Handle GET requests - ESP32 ENDPOINTS ONLY"""
        client_ip = self.client_address[0]
        print(f"[IN] [GET] {self.path} from {client_ip}", flush=True)
        # Root status endpoint
        if self.path == "/" or self.path == "/status":
            response = {
                "status": "success",
                "message": "[BUS] Python Backend - ESP32 Processing Engine Only",
                "bus_id": bus_tracker.bus_id,
                "route_name": bus_tracker.route_name,
                "esp32_endpoints": {
                    "GET": ["/api/health", "/api/trip-context", "/api/power-config"],
                    "POST": [
                        "/api/entry-logs",
                        "/api/exit-logs",
                        "/api/extract-face-embedding",
                    ],
                },
                "note": "All frontend CRUD endpoints moved to Node.js backend (port 5000)",
                "timestamp": datetime.now().isoformat(),
            }
            self._send_json_response(response)

        # ESP32 Health Check Endpoint
        elif self.path == "/api/health":
            response = {
                "status": "healthy",
                "timestamp": datetime.now().isoformat(),
                "bus_id": bus_tracker.bus_id,
            }
            self._send_json_response(response)

        # ESP32 Trip Context Endpoint - OPTIMIZED
        elif self.path.startswith("/api/trip-context"):
            try:
                query_params = self._get_query_params()
                bus_id = query_params.get("bus_id", [bus_tracker.bus_id])[0]
                esp32_trip_start = query_params.get("trip_start", [None])[0]
                esp32_trip_end = query_params.get("trip_end", [None])[0]

                # Get current time
                current_time = datetime.now()
                current_time_str = current_time.strftime("%H:%M")

                # AUTOMATIC TRIP MANAGEMENT based on ESP32 schedule
                if esp32_trip_start and esp32_trip_end:
                    print(
                        f"ESP32 provided schedule: {esp32_trip_start} - {esp32_trip_end}",
                        flush=True,
                    )

                    try:
                        # Check if current time is within ESP32 trip schedule
                        is_esp32_trip_time = bus_tracker.is_within_trip_schedule(
                            current_time_str, esp32_trip_start, esp32_trip_end
                        )

                        # Get current trip for THIS specific bus
                        current_trip = bus_tracker.get_current_trip(bus_id=bus_id)

                        if is_esp32_trip_time:
                            # Within ESP32 trip time - ensure we have an active trip
                            if (
                                not current_trip
                                or current_trip.get("status") != "active"
                            ):
                                print(
                                    f"AUTO-START: Creating trip for ESP32 schedule {esp32_trip_start}-{esp32_trip_end} on {bus_id}",
                                    flush=True,
                                )
                                # FIXED: Only clean entries for the SPECIFIC trip that is ending
                                # User Request: "temp entries not clean fully only clean that bus id and trip id is smae"
                                if current_trip:
                                    print(
                                        f"cleaning old entries for {bus_id} (Trip: {current_trip['trip_id']})...",
                                        flush=True,
                                    )
                                    bus_tracker.cleanup_old_temp_entries(
                                        hours_old=0,
                                        bus_id=bus_id,
                                        trip_id=current_trip["trip_id"],
                                    )
                                else:
                                    # If no active trip found, do NOT wipe everything (prevent race conditions)
                                    # Background task will clean orphans later.
                                    print(
                                        f"Skipping cleanup for {bus_id} (No active trip to close).",
                                        flush=True,
                                    )

                                # Auto-start trip for this specific bus
                                print(f"starting new trip for {bus_id}...", flush=True)
                                bus_tracker.start_new_trip(bus_id=bus_id)
                                current_trip = bus_tracker.get_current_trip(
                                    bus_id=bus_id
                                )
                                print(
                                    f"trip started: {current_trip.get('trip_id') if current_trip else 'NONE'}",
                                    flush=True,
                                )
                        else:
                            # Outside ESP32 trip time - end active trip if exists
                            if current_trip and current_trip.get("status") == "active":
                                print(
                                    f"AUTO-END: Trip ended for {bus_id} - outside ESP32 schedule {esp32_trip_start}-{esp32_trip_end}",
                                    flush=True,
                                )
                                # FIXED: Pass bus_id to end trip for the correct bus
                                bus_tracker.end_current_trip(bus_id=bus_id)
                                current_trip = None
                    except Exception as inner_e:
                        print(f"CRASH in auto-mgmt logic: {inner_e}", flush=True)
                        import traceback

                        traceback.print_exc()
                        raise inner_e  # Re-raise to be caught by outer handler
                else:
                    # Fallback to existing logic without ESP32 schedule
                    current_trip = bus_tracker.get_current_trip(bus_id=bus_id)
                    esp32_trip_start = "06:00"  # Default
                    esp32_trip_end = "18:00"  # Default

                # Build response
                if current_trip and current_trip.get("trip_id"):
                    # Active trip
                    response = {
                        "trip_id": current_trip["trip_id"],
                        "route_name": current_trip.get("route_name", "Jaffna-Colombo"),
                        "departure_city": current_trip.get("departure_city", "Colombo"),
                        "destination_city": current_trip.get(
                            "destination_city", "Jaffna"
                        ),
                        "schedule_start": esp32_trip_start,
                        "schedule_end": esp32_trip_end,
                        "trip_active": True,
                        "trip_status": "active",
                        "trip_date": datetime.now().strftime("%Y-%m-%d"),
                        "bus_id": bus_id,
                        "current_time": current_time.strftime("%H:%M:%S"),
                        "passengers_inside": current_trip.get("passengers_inside", 0),
                        "duration_minutes": current_trip.get("duration_minutes", 0),
                        "auto_managed": True
                        if esp32_trip_start and esp32_trip_end
                        else False,
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
                        "auto_managed": True
                        if esp32_trip_start and esp32_trip_end
                        else False,
                    }

                print(
                    f"Trip context: {response['trip_status']} | Auto: {response.get('auto_managed', False)}",
                    flush=True,
                )
                self._send_json_response(response)
            except Exception as e:
                import traceback

                print(f"ERROR in trip-context: {e}", flush=True)
                traceback.print_exc()
                self._send_error_response(f"Trip context error: {str(e)}", 500)

        # ESP32 Power Config Endpoint
        elif self.path.startswith("/api/power-config"):
            try:
                query_params = self._get_query_params()
                bus_id = query_params.get("bus_id", [None])[0]

                if not bus_id:
                    self._send_error_response("bus_id parameter required", 400)
                    return

                config = get_power_config(bus_id)
                if config:
                    response = {
                        "bus_id": config["bus_id"],
                        "deep_sleep_enabled": config.get("deep_sleep_enabled", True),
                        "trip_start": config.get("trip_start", "00:00"),
                        "trip_end": config.get("trip_end", "23:59"),
                        "smart_power_enabled": config.get("smart_power_enabled", False),
                        "trip_windows": config.get("trip_windows", []),
                        "maintenance_interval": config.get("maintenance_interval", 5),
                        "maintenance_duration": config.get("maintenance_duration", 3),
                        "boards": config.get("boards", []),
                        "last_updated": config["last_updated"].isoformat()
                        if isinstance(config.get("last_updated"), datetime)
                        else config.get("last_updated"),
                        "current_server_time": datetime.now().strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                    }
                    self._send_json_response(response)
                else:
                    self._send_error_response("Failed to get config", 404)
            except Exception as e:
                self._send_error_response(str(e))

        # All other endpoints removed - use Node.js backend
        else:
            self._send_error_response(
                "Endpoint not found. Use Node.js backend for frontend APIs.", 404
            )

    def do_POST(self):
        """Handle POST requests - ESP32 ENDPOINTS ONLY"""
        client_ip = self.client_address[0]
        print(f"[IN] [POST] {self.path} from {client_ip}", flush=True)
        try:
            parsed_path = urlparse(self.path)

            # ESP32 Face Embedding Extraction Endpoint
            if parsed_path.path in ("/api/extract-face-embedding", "/api/python/api/extract-face-embedding"):
                content_length = int(self.headers.get("Content-Length", 0))
                print(
                    f"[PHOTO] Incoming face photo: {content_length / 1024:.1f} KB",
                    flush=True,
                )

                # Read data
                post_data = self.rfile.read(content_length)
                print(f"[IN] Data received, parsing JSON...", flush=True)

                data = json.loads(post_data.decode("utf-8"))
                image_data = data.get("image_data", "")
                print(f"[SEARCH] Starting face extraction...", flush=True)

                # Process using the pre-loaded helper
                try:
                    result = extract_face_embedding_from_base64(image_data)
                except ImportError:
                    print(
                        "[WARN] face_recognition_helper not found, using fallback",
                        flush=True,
                    )
                    # Fallback to mock embedding
                    import hashlib

                    image_hash = hashlib.md5(image_data.encode()).hexdigest()
                    mock_embedding = [
                        float(int(image_hash[i : i + 2], 16)) / 255.0
                        for i in range(0, 32, 2)
                    ]
                    mock_embedding = mock_embedding * 8

                    result = {
                        "success": True,
                        "face_embedding": mock_embedding,
                        "embedding_size": len(mock_embedding),
                        "num_faces": 1,
                        "message": "MOCK embedding (face_recognition_helper not found)",
                        "is_mock": True,
                    }

                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

                response = {**result, "timestamp": datetime.now().isoformat()}
                self.wfile.write(json.dumps(response, indent=2).encode())
                return

            # ESP32 Device Health Endpoint
            elif parsed_path.path == "/api/device-health":
                content_length = int(self.headers.get("Content-Length", 0))
                post_data = self.rfile.read(content_length)

                data = json.loads(post_data.decode("utf-8"))
                device_id = data.get("device_id", "UNKNOWN")
                bus_id = data.get("bus_id", "UNKNOWN")

                print(
                    f"[HEALTH] ESP32 Health report from {device_id} ({bus_id})",
                    flush=True,
                )

                # Print key health metrics
                if "health" in data:
                    health = data["health"]
                    print(
                        f"   [WIFI] WiFi: {health.get('wifi_status', False)} (RSSI: {health.get('wifi_rssi', 0)})",
                        flush=True,
                    )
                    print(
                        f"   [CAM] Camera: {health.get('camera_status', False)}",
                        flush=True,
                    )
                    print(
                        f"   [GPS] GPS: {health.get('gps_status', False)} ({health.get('gps_satellite_count', 0)} sats)",
                        flush=True,
                    )
                    print(
                        f"   [MEM] Memory: {health.get('free_heap_bytes', 0):,} bytes free",
                        flush=True,
                    )

                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()

                response = {
                    "status": "received",
                    "message": "Health report stored successfully",
                    "device_id": device_id,
                    "timestamp": datetime.now().isoformat(),
                }
                self.wfile.write(json.dumps(response, indent=2).encode())

            # ESP32 Face Detection Endpoints (Entry/Exit)
            elif parsed_path.path in [
                "/api/entry-logs",
                "/api/exit-logs",
                "/api/face-logs",
            ]:
                content_length = int(self.headers.get("Content-Length", 0))
                post_data = self.rfile.read(content_length)

                json_data = json.loads(post_data.decode("utf-8"))
                device_id = json_data.get("device_id", "unknown")
                bus_id = json_data.get(
                    "bus_id", bus_tracker.default_bus_id
                )  # MULTI-BUS: Extract bus_id
                logs = json_data.get("logs", [])

                # Determine location type from endpoint
                if parsed_path.path == "/api/entry-logs":
                    location_type = "ENTRY"
                elif parsed_path.path == "/api/exit-logs":
                    location_type = "EXIT"
                else:
                    location_type = (
                        logs[0].get("location_type", "UNKNOWN") if logs else "UNKNOWN"
                    )

                print(f"\n[BUS] ESP32 Face Detection Data Received", flush=True)
                print(f"Bus: {bus_id}", flush=True)  # MULTI-BUS: Show bus_id
                print(f"Device: {device_id}", flush=True)
                print(f"Type: {location_type}", flush=True)
                print(f"Logs: {len(logs)}", flush=True)

                results = []
                for i, log in enumerate(logs):
                    # Add location_type and bus_id to the log entry
                    log["location_type"] = location_type
                    log["bus_id"] = bus_id  # MULTI-BUS: Add bus_id to each log

                    print(
                        f"\n[LOC] Processing: {location_type} on {bus_id} - Face ID: {log.get('face_id')}",
                        flush=True,
                    )

                    # --- VALIDATION: Bus ID and Time ---
                    # 1. Validate Bus ID
                    if not bus_tracker.power_configs.find_one({"bus_id": bus_id}):
                        print(f"[WARN] REJECTING: Unknown Bus ID {bus_id}", flush=True)
                        results.append(
                            {"action": "rejected", "message": "Unknown Bus ID"}
                        )
                        continue

                    # 2. Validate Time (Prevent 1970/default dates)
                    log_time_str = log.get("timestamp")
                    parsed_time = bus_tracker._parse_timestamp_safe(log_time_str)
                    if parsed_time.year < 2024:
                        print(
                            f"[WARN] REJECTING: Invalid timestamp {parsed_time} (System time not synced?)",
                            flush=True,
                        )
                        results.append(
                            {"action": "rejected", "message": "Invalid Timestamp"}
                        )
                        continue

                    # 3. Validate Trip Window (STRICT SCHEDULE CHECK)
                    # REQ: Store ONLY if bus_id schedule time is correct
                    is_in_window = False
                    if schedule_manager:
                        # Fetch the dynamic schedule for this specific bus
                        schedule_doc = schedule_manager.bus_schedules.find_one(
                            {"bus_id": bus_id, "active": True}
                        )

                        if schedule_doc:
                            # Convert UTC log time to Local Time for comparison with Local Schedule
                            local_time = parsed_time + timedelta(
                                hours=bus_tracker.timezone_offset_hours
                            )
                            log_time_hhmm = local_time.strftime("%H:%M")
                            today_name = local_time.strftime("%A").lower()

                            print(
                                f"[CLOCK] Checking schedule: Log UTC {parsed_time.strftime('%H:%M')} -> Local {log_time_hhmm} vs trips",
                                flush=True,
                            )

                            for trip in schedule_doc.get("trips", []):
                                if not trip.get("active", True):
                                    continue

                                # Get window: Boarding Start -> Arrival + Stop Duration
                                start_t = trip.get("boarding_start_time", "06:00")
                                arrival_t = trip.get("estimated_arrival_time", start_t)
                                stop_mins = trip.get("stop_duration_minutes", 30)

                                try:
                                    arrival_dt = datetime.strptime(arrival_t, "%H:%M")
                                    end_dt = arrival_dt + timedelta(minutes=stop_mins)
                                    end_t = end_dt.strftime("%H:%M")

                                    # Use cross-day aware comparison
                                    if bus_tracker.is_within_trip_schedule(
                                        log_time_hhmm, start_t, end_t
                                    ):
                                        is_in_window = True
                                        break
                                except Exception as e:
                                    print(
                                        f"[WARN] Error parsing schedule window for {bus_id}: {e}",
                                        flush=True,
                                    )
                                    continue
                        else:
                            print(
                                f"[WARN] No active schedule found in MongoDB for bus {bus_id}",
                                flush=True,
                            )

                    if not is_in_window:
                        check_time = (
                            local_time.strftime("%H:%M")
                            if "local_time" in locals()
                            else parsed_time.strftime("%H:%M")
                        )

                        if bus_tracker.debug_allow_all_logs:
                            print(
                                f"[DEBUG] DEBUG BYPASS: Log for {bus_id} at {check_time} (Local) accepted despite schedule.",
                                flush=True,
                            )
                            is_in_window = True
                        else:
                            print(
                                f"[ERROR] REJECTING: Log for {bus_id} at {check_time} (Local) is OUTSIDE scheduled trip hours.",
                                flush=True,
                            )
                            results.append(
                                {
                                    "action": "rejected",
                                    "message": f"Outside Scheduled Trip Hours (Log:{check_time})",
                                }
                            )
                            continue

                    print(
                        f"[OK] Log accepted: {bus_id} time is within scheduled window",
                        flush=True,
                    )
                    # -----------------------------------

                    # Process using existing system
                    result = bus_tracker.process_face_log(log)
                    results.append(result)

                    # Print details
                    face_id = log.get("face_id", "UNKNOWN")
                    timestamp = log.get("timestamp", "UNKNOWN")
                    lat = log.get("latitude", 0)
                    lon = log.get("longitude", 0)

                    print(
                        f"   Face {i + 1}: ID={face_id}, Time={timestamp}", flush=True
                    )
                    if lat != 0 or lon != 0:
                        print(f"           GPS: {lat:.6f}, {lon:.6f}", flush=True)

                    # Print processing result
                    if result.get("action") == "matched_journey":
                        print(f"           [OK] {result['message']}", flush=True)
                    elif result.get("action") == "stored_entry":
                        print(f"           [LOG] {result['message']}", flush=True)
                    elif result.get("action") == "unmatched_exit":
                        print(f"           [ERROR] {result['message']}", flush=True)

                # Send response
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()

                # Return summary
                matched_journeys = len(
                    [r for r in results if r.get("action") == "matched_journey"]
                )
                stored_entries = len(
                    [r for r in results if r.get("action") == "stored_entry"]
                )
                unmatched_exits = len(
                    [r for r in results if r.get("action") == "unmatched_exit"]
                )

                response = {
                    "status": "received",
                    "message": f"Processed {len(logs)} {location_type.lower()} logs for {bus_id}",
                    "log_count": len(logs),
                    "bus_id": bus_id,  # MULTI-BUS: Include bus_id in response
                    "device_id": device_id,
                    "processing_summary": {
                        "matched_journeys": matched_journeys,
                        "stored_entries": stored_entries,
                        "unmatched_exits": unmatched_exits,
                    },
                    "results": results,
                    "timestamp": datetime.now().isoformat(),
                }
                self.wfile.write(json.dumps(response, indent=2).encode())

            # ESP32 Board Heartbeat - Restored (Required for POWER_SYNC)
            elif parsed_path.path == "/api/board-heartbeat":
                content_length = int(self.headers.get("Content-Length", 0))
                post_data = self.rfile.read(content_length)

                try:
                    data = json.loads(post_data.decode("utf-8"))
                    device_id = data.get("device_id", "UNKNOWN")
                    target_bus_id = data.get("bus_id", bus_tracker.default_bus_id)

                    print(
                        f"[HEART] Heartbeat received from {device_id} (Bus: {target_bus_id})",
                        flush=True,
                    )

                    # Update DB with board status
                    try:
                        client_ip = self.client_address[0]
                        device_type = (
                            "ENTRANCE" if "ENTRANCE" in device_id.upper() else "EXIT"
                        )

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
                                    "updated_at": utc_now,
                                }
                            },
                        )

                        # 2. If not found (matched_count == 0), push new board
                        if result.matched_count == 0:
                            print(
                                f"[ADD] Registering new board: {device_id}", flush=True
                            )
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
                                            "added_at": utc_now,
                                        }
                                    },
                                    "$set": {"updated_at": utc_now},
                                },
                                upsert=True,
                            )

                    except Exception as db_err:
                        print(
                            f"[WARN] Failed to update board status in DB: {db_err}",
                            flush=True,
                        )

                    self.send_response(200)
                    self.send_header("Content-type", "application/json")
                    self.end_headers()
                    response = {
                        "status": "success",
                        "timestamp": datetime.now().isoformat(),
                    }
                    self.wfile.write(json.dumps(response).encode())
                    return
                except Exception as e:
                    print(f"[ERROR] Error processing heartbeat: {e}", flush=True)
                    self.send_response(400)
                    self.end_headers()
                    return

            else:
                self.send_response(404)
                self.end_headers()

        except Exception as e:
            print(f"[ERROR] Error processing request: {e}", flush=True)
            self.send_response(500)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            error_response = {"status": "error", "message": str(e)}
            self.wfile.write(json.dumps(error_response).encode())

    # DELETE handler removed - Not needed for ESP32 endpoints
    # All CRUD operations moved to Node.js backend


def run_background_checks():
    """
    TRIP LIFECYCLE MONITOR
    ──────────────────────
    Runs every 60 seconds and manages the three-phase trip lifecycle:

      active  ──(schedule window ends)──►  ending  ──(TRIP_END_GRACE_MINUTES)──►  close_trip_session()
                                                                                          │
                                                                    temp_entries (this trip only)
                                                                          │
                                                                    unmatchedPassengers
                                                                    status → "completed"

    Phase rules
    ───────────
    active  : normal operation; ENTRY + EXIT logs both accepted.
    ending  : grace window (TRIP_END_GRACE_MINUTES = 30 min).
              EXIT logs still accepted so late-alighting passengers are matched.
              ENTRY logs rejected (bus has arrived; no new boardings).
    completed: closed; no further matching.

    Hourly housekeeping also runs cleanup_old_temp_entries(hours_old=6) to
    catch any orphaned entries from abnormal shutdowns.
    """
    print("[BG] Starting trip lifecycle monitor...", flush=True)
    last_housekeeping = datetime.now() - timedelta(hours=1)

    while True:
        try:
            time.sleep(60)  # Scan every minute
            now = datetime.now()
            now_utc = datetime.utcnow()
            current_hhmm = now.strftime("%H:%M")

            # ── HOURLY HOUSEKEEPING: orphaned / very old temp entries ─────────
            if (now - last_housekeeping).total_seconds() > 3600:
                print(
                    f"[BG] Hourly housekeeping: cleaning orphaned temp entries "
                    f"older than 6 hours...",
                    flush=True,
                )
                bus_tracker.cleanup_old_temp_entries(hours_old=6)
                last_housekeeping = now

            # ── PHASE 1: active sessions → detect end-of-schedule ────────────
            # If an active session is outside its scheduled window it transitions
            # to "ending" (starts the 30-minute grace period).
            # We do NOT call close_trip_session() directly here so that EXIT logs
            # arriving after the schedule window still get a chance to be matched.
            active_sessions = list(bus_tracker.trip_sessions.find({"status": "active"}))

            for session in active_sessions:
                bus_id = session.get("bus_id")
                trip_id = session.get("trip_id")
                session_start = session.get("start_time")

                if not bus_id or not trip_id or not session_start:
                    continue

                if not schedule_manager:
                    # No schedule manager configured — skip automatic ending.
                    continue

                windows = schedule_manager.get_todays_trip_windows(bus_id)

                if not windows:
                    # No windows defined for this bus today.
                    # If the session has been running for more than TRIP_END_GRACE_MINUTES
                    # it is likely a stale leftover from yesterday → start grace period.
                    age_seconds = (now_utc - session_start).total_seconds()
                    if age_seconds > TRIP_END_GRACE_MINUTES * 60:
                        print(
                            f"[BG] Session {trip_id} ({bus_id}): no schedule windows "
                            f"today and running for {age_seconds / 60:.0f} min. "
                            f"Starting {TRIP_END_GRACE_MINUTES}-min grace period.",
                            flush=True,
                        )
                        bus_tracker.mark_trip_ending(bus_id, trip_id)
                    continue

                # Check whether current time falls inside any scheduled window.
                is_active_time = False
                for window in windows:
                    if bus_tracker.is_within_trip_schedule(
                        current_hhmm, window["start_time"], window["end_time"]
                    ):
                        is_active_time = True
                        break

                if not is_active_time:
                    # Must have been running for at least 15 min before we act
                    # (guards against false triggers at the very start of a session).
                    age_seconds = (now_utc - session_start).total_seconds()
                    if age_seconds > 900:
                        print(
                            f"[BG] Session {trip_id} ({bus_id}): outside scheduled "
                            f"window. Starting {TRIP_END_GRACE_MINUTES}-min grace period.",
                            flush=True,
                        )
                        bus_tracker.mark_trip_ending(bus_id, trip_id)

            # ── PHASE 2: ending sessions → wait for grace period, then close ─
            # When TRIP_END_GRACE_MINUTES have elapsed since end_detected_at,
            # call close_trip_session() which:
            #   1. Moves all remaining temp_entries for THIS trip → unmatchedPassengers
            #   2. Marks the trip as "completed"
            ending_sessions = list(bus_tracker.trip_sessions.find({"status": "ending"}))

            for session in ending_sessions:
                bus_id = session.get("bus_id")
                trip_id = session.get("trip_id")
                end_detected_at = session.get("end_detected_at")

                if not bus_id or not trip_id or not end_detected_at:
                    # Safety: if end_detected_at is missing, close it now.
                    if bus_id and trip_id:
                        print(
                            f"[BG] Session {trip_id} ({bus_id}): missing "
                            f"end_detected_at — closing immediately.",
                            flush=True,
                        )
                        bus_tracker.close_trip_session(bus_id, trip_id)
                    continue

                grace_elapsed_seconds = (now_utc - end_detected_at).total_seconds()
                grace_elapsed_minutes = grace_elapsed_seconds / 60.0
                remaining_minutes = TRIP_END_GRACE_MINUTES - grace_elapsed_minutes

                if grace_elapsed_seconds >= TRIP_END_GRACE_MINUTES * 60:
                    print(
                        f"[BG] Session {trip_id} ({bus_id}): grace period elapsed "
                        f"({grace_elapsed_minutes:.1f} min). "
                        f"Moving unmatched temp_entries → unmatchedPassengers...",
                        flush=True,
                    )
                    bus_tracker.close_trip_session(bus_id, trip_id)
                else:
                    print(
                        f"[BG] Session {trip_id} ({bus_id}): in grace period — "
                        f"auto-close in {remaining_minutes:.1f} min.",
                        flush=True,
                    )

        except Exception as e:
            print(f"[BG] Error in trip lifecycle monitor: {e}", flush=True)
            # Don't crash the thread — log and continue to next iteration.


def run_server(port=None):
    """Run the ESP32 processing backend"""
    # Use environment variable PORT for production deployment
    if port is None:
        port = int(os.environ.get("PORT", 8888))

    server_address = ("0.0.0.0", port)  # Bind to 0.0.0.0 for external access
    httpd = ThreadingHTTPServer(server_address, SimplifiedHandler)

    print(f"\n{'=' * 70}", flush=True)
    print(
        f"[BUS] Python Backend - ESP32 Processing Engine (MULTI-BUS ENABLED)",
        flush=True,
    )
    print(f"{'=' * 70}", flush=True)
    print(
        f"[LOC] Default Bus: {bus_tracker.default_bus_id} ({bus_tracker.route_name})",
        flush=True,
    )
    print(f"[SYNC] Multi-bus support: Accepts bus_id from ESP32 requests", flush=True)
    print(f" Server running on port {port}", flush=True)
    print(f"Press Ctrl+C to stop the server", flush=True)
    print(f"{'=' * 70}\n", flush=True)

    # Start background monitor
    monitor_thread = threading.Thread(target=run_background_checks, daemon=True)
    monitor_thread.start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[STOP] Server stopped", flush=True)
        httpd.server_close()


if __name__ == "__main__":
    run_server()
