from __future__ import annotations
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import Trip

from sqlalchemy import select, func  # make sure this is imported at the top
from math import radians, sin, cos, sqrt, atan2
from app.models.db_models import Trip, TripData  # if not already imported


async def _compute_trip_stats(
    db: AsyncSession,
    trip_id: str,
) -> dict:
    """
    Compute total_distance (km), average_speed (km/h), max_speed (km/h),
    average_heart_rate and max_heart_rate for a given trip.

    Uses ordered TripData points (lat/lng + timestamp + heart_rate).
    """
    res = await db.execute(
        select(TripData)
        .where(TripData.trip_id == trip_id)
        .order_by(TripData.timestamp.asc())
    )
    points = list(res.scalars().all())

    if len(points) < 2:
        # Not enough points to compute distance/speed
        avg_hr = None
        max_hr = None
        if points and points[0].heart_rate is not None:
            avg_hr = float(points[0].heart_rate)
            max_hr = float(points[0].heart_rate)

        return {
            "total_distance": 0.0,
            "average_speed": 0.0,
            "max_speed": 0.0,
            "average_heart_rate": avg_hr,
            "max_heart_rate": max_hr,
        }

    # Haversine distance in meters between two lat/lng pairs
    def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R = 6371000.0  # Earth radius in meters
        phi1 = radians(lat1)
        phi2 = radians(lat2)
        dphi = radians(lat2 - lat1)
        dlambda = radians(lon2 - lon1)

        a = sin(dphi / 2.0) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2.0) ** 2
        c = 2 * atan2(sqrt(a), sqrt(1 - a))
        return R * c

    total_distance_m = 0.0
    max_speed_kmh = 0.0

    prev = points[0]
    # Updated loop with speed_kmh support
    # We iterate and calculate speed for each segment OR use provided speed
    
    for current in points[1:]:
        seg_speed_kmh = 0.0
        
        # 1. Prefer explicit velocity
        if current.speed_kmh is not None:
             seg_speed_kmh = float(current.speed_kmh)
             # We still calc distance for total_distance using GPS (Req: "distance remains computed from GPS")
        
        # 2. GPS Fallback / Distance Calculation
        dist_segment_m = 0.0
        if (
            prev.lat is not None and prev.lng is not None
            and current.lat is not None and current.lng is not None
        ):
            dist_segment_m = haversine_m(prev.lat, prev.lng, current.lat, current.lng)
            
            # If no explicit speed, calc from GPS
            if current.speed_kmh is None:
                dt = (current.timestamp - prev.timestamp).total_seconds()
                if dt > 0.5: # tolerate small drifts
                     seg_speed_kmh = (dist_segment_m / dt) * 3.6
        
        # Accumulate
        total_distance_m += dist_segment_m
        if seg_speed_kmh > max_speed_kmh:
            max_speed_kmh = seg_speed_kmh
            
        prev = current

    # Total duration based on timestamps
    total_duration_s = (points[-1].timestamp - points[0].timestamp).total_seconds()
    if total_duration_s > 0:
        avg_speed_kmh = (total_distance_m / total_duration_s) * 3.6
    else:
        avg_speed_kmh = 0.0

    # Distance in kilometers for storage
    total_distance_km = total_distance_m / 1000.0

    # Heart-rate stats
    hr_values = [p.heart_rate for p in points if p.heart_rate is not None]
    if hr_values:
        avg_hr = float(sum(hr_values) / len(hr_values))
        max_hr = float(max(hr_values))
    else:
        avg_hr = None
        max_hr = None

    return {
        "total_distance": total_distance_km,
        "average_speed": avg_speed_kmh,
        "max_speed": max_speed_kmh,
        "average_heart_rate": avg_hr,
        "max_heart_rate": max_hr,
    }



# -------------------------------
# CREATE / CLOSE / FETCH TRIPS
# -------------------------------

async def create_trip(
    db: AsyncSession,
    user_id: Optional[str],
    device_id: str,
    start_time: datetime,
    start_lat: Optional[float] = None,
    start_lng: Optional[float] = None,
) -> Trip:
    """
    Create a new trip entry when a trip_start message arrives.
    Returns the Trip object (not yet committed).
    """
    trip = Trip(
        user_id=user_id,
        device_id=device_id,
        active_key=device_id,
        start_time=start_time,
        start_lat=start_lat,
        start_lng=start_lng,
        status="recording",
    )
    db.add(trip)
    await db.flush()  # to get trip_id populated
    return trip

async def close_trip(
    db: AsyncSession,
    trip_id: str,
    end_time: datetime,
    end_lat: Optional[float] = None,
    end_lng: Optional[float] = None,
    crash_detected: Optional[bool] = None,
) -> None:
    """
    Mark a trip as completed (called when trip_end message arrives)
    and compute distance/speed/heart-rate stats.
    """
    stats = await _compute_trip_stats(db, trip_id)

    await db.execute(
        update(Trip)
        .where(Trip.trip_id == trip_id)
        .values(
            end_time=end_time,
            crash_detected=crash_detected,
            status="completed",
            total_distance=stats["total_distance"],
            average_speed=stats["average_speed"],
            max_speed=stats["max_speed"],
            average_heart_rate=stats["average_heart_rate"],
            max_heart_rate=stats["max_heart_rate"],
            active_key=None,
            updated_at=datetime.utcnow(),
        )
    )
    # keep commit control at the caller level, like before

async def cancel_trip(db: AsyncSession, trip_id: str, end_time: datetime) -> None:
    """Force-cancel a trip (if aborted)."""
    await db.execute(
        update(Trip)
        .where(Trip.trip_id == trip_id)
        .values(
            status="cancelled",
            end_time=end_time,
            active_key=None,
            updated_at=datetime.utcnow(),
        )
    )


# -------------------------------
# FETCHING TRIPS
# -------------------------------

async def get_active_trip_for_device(db: AsyncSession, device_id: str) -> Optional[Trip]:
    """
    Return the currently active trip (recording) for a given device.
    Used when telemetry arrives without a trip_id.
    """
    res = await db.execute(
        select(Trip)
        .where(Trip.device_id == device_id, Trip.status == "recording")
        .order_by(Trip.start_time.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


async def get_trip_by_id(db: AsyncSession, trip_id: str) -> Optional[Trip]:
    """Fetch a trip by its ID."""
    res = await db.execute(select(Trip).where(Trip.trip_id == trip_id))
    return res.scalar_one_or_none()


async def list_trips_for_user(
    db: AsyncSession,
    user_id: str,
    limit: int = 50,
    offset: int = 0,
) -> Sequence[Trip]:
    """List trips for a user (for history screens)."""
    q = (
        select(Trip)
        .where(Trip.user_id == user_id)
        .order_by(Trip.start_time.desc())
        .limit(limit)
        .offset(offset)
    )
    res = await db.execute(q)
    return tuple(res.scalars().all())




# | Function                       | What it does                                                                          | Used by              |
# | ------------------------------ | ------------------------------------------------------------------------------------- | -------------------- |
# | `create_trip()`                | Creates a new trip when a `trip_start` arrives (sets status=recording).               | persistence worker   |
# | `close_trip()`                 | Marks a trip as completed on `trip_end`.                                              | persistence worker   |
# | `cancel_trip()`                | Cancels a trip (for errors or user aborts).                                           | optional admin route |
# | `get_active_trip_for_device()` | Finds the open trip for a helmet; used when telemetry arrives but no trip_id is sent. | persistence worker   |
# | `get_trip_by_id()`             | Fetches a single trip by ID (for APIs or debugging).                                  | API route            |
# | `list_trips_for_user()`        | Lists all trips for a specific user (used for history pages).                         | `/api/v1/trips`      |

from app.models.db_models import TripData

class TripsRepo:
    """
    Static wrapper class for better import usage in endpoints.
    """
    @staticmethod
    async def get_trip(db: AsyncSession, trip_id: str) -> Optional[Trip]:
        return await get_trip_by_id(db, trip_id)

    @staticmethod
    async def get_user_trips(db: AsyncSession, user_id: str, limit: int = 50, offset: int = 0) -> Sequence[Trip]:
        return await list_trips_for_user(db, user_id, limit, offset)

    @staticmethod
    async def get_trip_route_points(db: AsyncSession, trip_id: str) -> Sequence[TripData]:
        """
        Fetch GPS points for a trip, ordered by time.
        Only returns points with valid lat/lng.
        """
        q = (
            select(TripData)
            .where(
                TripData.trip_id == trip_id,
                TripData.lat.is_not(None),
                TripData.lng.is_not(None)
            )
            .order_by(TripData.timestamp.asc())
        )
        res = await db.execute(q)
        return tuple(res.scalars().all())

    @staticmethod
    async def get_last_known_location(db: AsyncSession, trip_id: str) -> Optional[TripData]:
        """
        Fetch the most recent TripData with valid lat/lng for a trip.
        Used to set end_lat/end_lng when auto-closing.
        """
        q = (
            select(TripData)
            .where(
                TripData.trip_id == trip_id,
                TripData.lat.is_not(None),
                TripData.lng.is_not(None)
            )
            .order_by(TripData.timestamp.desc())
            .limit(1)
        )
        res = await db.execute(q)
        return res.scalar_one_or_none()

    @staticmethod
    async def get_daily_aggregates(db: AsyncSession, user_id: str, date_str: str) -> dict:
        """
        Get aggregated stats for a specific day (date_str: YYYY-MM-DD).
        """
        # Cast start_time to DATE to compare with string YYYY-MM-DD
        # or use a range check
        
        q = (
            select(
                func.avg(Trip.average_heart_rate).label("avg_hr"),
                func.max(Trip.max_heart_rate).label("max_hr"),
                func.avg(Trip.average_speed).label("avg_speed"),
                func.sum(Trip.total_distance).label("total_dist"),
                func.count(Trip.trip_id).label("trip_count")
            )
            .where(
                Trip.user_id == user_id,
                func.date(Trip.start_time) == date_str,
                Trip.status == "completed"
            )
        )
        
        res = await db.execute(q)
        row = res.one() # Should always return one row with nulls if no match
        
        return {
            "date": date_str,
            "average_heart_rate": row.avg_hr or 0.0,
            "max_heart_rate": row.max_hr or 0.0,
            "average_speed": row.avg_speed or 0.0,
            "total_distance": row.total_dist or 0.0,
            "total_trips": row.trip_count
        }
