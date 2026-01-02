from typing import Optional, Dict, Any
from math import radians, sin, cos, sqrt, atan2
from datetime import datetime

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great circle distance between two points 
    on the earth (specified in decimal degrees) in Kilometers.
    """
    R = 6371.0  # Earth radius in km
    phi1 = radians(lat1)
    phi2 = radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)

    a = sin(dphi / 2.0) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2.0) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c

def get_speed_kmh(telemetry_msg: Dict[str, Any], last_gps_msg: Optional[Dict[str, Any]]) -> Optional[float]:
    """
    Determine the speed in km/h from telemetry.
    1. Prefer 'velocity.kmh' if present and valid.
    2. Fallback to GPS calc if 'last_gps_msg' is provided and valid.
    3. Return None otherwise.
    """
    # 1. Try Velocity
    velocity = telemetry_msg.get("velocity")
    if velocity and isinstance(velocity, dict):
        v_kmh = velocity.get("kmh")
        if v_kmh is not None:
            # We assume schema validation already clamped/checked validity, 
            # but extra check is cheap.
            if 0 <= v_kmh <= 250:
                return float(v_kmh)

    # 2. GPS Fallback
    current_gps = telemetry_msg.get("gps")
    if (
            last_gps_msg
    and current_gps
    and current_gps.get("ok")
    and last_gps_msg.get("ok", True)  # allow old dicts without ok
    and last_gps_msg.get("lat") is not None
    and last_gps_msg.get("lng") is not None
    and current_gps.get("lat") is not None
    and current_gps.get("lng") is not None
    ):
        
        try:
            # Parse timestamps if string, or assume objects
            # TelemetryIn converts ts to datetime, but here we might work with dictsdicts
            ts1 = last_gps_msg.get("ts")
            ts2 = telemetry_msg.get("ts")
            
            # Helper to normalize to datetime
            def to_dt(t):
                if isinstance(t, str):
                    return datetime.fromisoformat(t.replace('Z', '+00:00'))
                return t
            
            t1_dt = to_dt(ts1)
            t2_dt = to_dt(ts2)
            
            delta_sec = (t2_dt - t1_dt).total_seconds()
            
            if delta_sec > 0.5: # avoid div by zero or tiny deltas
                dist_km = haversine_km(
                    last_gps_msg["lat"], last_gps_msg["lng"],
                    current_gps["lat"], current_gps["lng"]
                )
                speed_calc = (dist_km / delta_sec) * 3600.0
                return speed_calc
                
        except Exception:
            pass
            
    return None
