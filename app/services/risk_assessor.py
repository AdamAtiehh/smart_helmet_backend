import math
from typing import List, Dict, Any, Optional
from datetime import datetime

from app.services.speed_utils import get_speed_kmh

class RiskAssessor:

    @classmethod
    def assess_risk(cls, window_msgs: List[Dict[str, Any]], last_gps: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Assess driving risk based on a window of telemetry messages.
        
        window_msgs: List of telemetry dicts (latest at end)
        last_gps: Start of window GPS context (lat, lng, ts) or None
        
        Returns a dict payload for RISK_STATUS.
        """
        if not window_msgs:
            return {
                "level": "NORMAL",
                "score": 0,
                "reasons": [],
                "speed_kmh": None
            }

        score = 0
        reasons = []
        
        latest_msg = window_msgs[-1]

        # 1. Aggressive Maneuvers (IMU), Check aggressive behavior in the window
        high_gyro_count = 0
        accel_spike_detected = False

        for msg in window_msgs:
            imu = msg.get("imu", {})
            
            # Gyro magnitude
            gx, gy, gz = imu.get("gx", 0.0), imu.get("gy", 0.0), imu.get("gz", 0.0)
            gyro_mag = math.sqrt(gx**2 + gy**2 + gz**2)
            if gyro_mag > 3.5:
                high_gyro_count += 1
            
            # Accel magnitude
            ax, ay, az = imu.get("ax", 0.0), imu.get("ay", 0.0), imu.get("az", 0.0)
            acc_mag = math.sqrt(ax**2 + ay**2 + az**2)
            # Normal gravity is ~9.8. Spike > 16.
            if acc_mag > 16.0: 
                accel_spike_detected = True

        if high_gyro_count > 3:
            score += 20
            reasons.append("swerving")
        
        if accel_spike_detected:
            score += 20
            reasons.append("sudden_movement")

        # 2. Speeding (Unified Source), Use util that prefers velocity.kmh > GPS delta
        speed_kmh = get_speed_kmh(latest_msg, last_gps)
        
        if speed_kmh is not None:
            if speed_kmh > 60:
                score += 30
                reasons.append("speeding")
            elif speed_kmh > 45:
                score += 10 # slightly risky

        # 3. Heart Rate
        hr = latest_msg.get("heart_rate", {}).get("hr", 0)
        if hr > 120:
            score += 15
            reasons.append("high_hr")
        
        # Clamp score
        score = min(100, max(0, score))
        
        # Determine Level
        if score >= 70:
            level = "DANGEROUS"
        elif score >= 40:
            level = "RISKY"
        else:
            level = "NORMAL"

        return {
            "level": level,
            "score": score,
            "reasons": reasons,
            "speed_kmh": round(speed_kmh, 1) if speed_kmh is not None else None
        }
