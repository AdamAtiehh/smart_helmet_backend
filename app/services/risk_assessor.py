import math
import statistics
from typing import List, Dict, Any, Optional

from app.services.speed_utils import get_speed_kmh


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _percentile(sorted_vals: List[float], p: float) -> float:
    """
    p in [0,100]
    sorted_vals must be sorted and non-empty
    """
    if not sorted_vals:
        return 0.0
    if p <= 0:
        return float(sorted_vals[0])
    if p >= 100:
        return float(sorted_vals[-1])

    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(math.floor(k))
    c = int(math.ceil(k))
    if f == c:
        return float(sorted_vals[f])
    d0 = sorted_vals[f] * (c - k)
    d1 = sorted_vals[c] * (k - f)
    return float(d0 + d1)


class RiskAssessor:
    @classmethod
    def assess_risk(
        cls,
        window_msgs: List[Dict[str, Any]],
        last_gps: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Dashboard risk status + ML gating signal.
        - Keeps legacy keys: level, score, reasons, speed_kmh
        - Adds: ml_gate, ml_gate_reasons, ml_gate_debug
        """
        if not window_msgs:
            return {
                "level": "NORMAL",
                "score": 0,
                "reasons": [],
                "speed_kmh": None,
                "ml_gate": False,
                "ml_gate_reasons": [],
                "ml_gate_debug": {},
            }

        latest_msg = window_msgs[-1]

        # -----------------------------
        # Collect IMU magnitudes (valid only)
        # -----------------------------
        acc_mags_raw: List[float] = []
        gyro_mags: List[float] = []

        for msg in window_msgs:
            imu = msg.get("imu") or {}
            if not isinstance(imu, dict):
                continue
            if imu.get("ok") is False:
                continue
            if imu.get("sleep") is True:
                continue

            ax = _safe_float(imu.get("ax")) or 0.0
            ay = _safe_float(imu.get("ay")) or 0.0
            az = _safe_float(imu.get("az")) or 0.0

            gx = _safe_float(imu.get("gx")) or 0.0
            gy = _safe_float(imu.get("gy")) or 0.0
            gz = _safe_float(imu.get("gz")) or 0.0

            acc_mags_raw.append(math.sqrt(ax * ax + ay * ay + az * az))
            gyro_mags.append(math.sqrt(gx * gx + gy * gy + gz * gz))

        # -----------------------------
        # Determine accel units (g vs m/s^2)
        # Your real data is ~1.0 => g units.
        # -----------------------------
        acc_mags_g: List[float] = []
        if acc_mags_raw:
            acc_med = statistics.median(acc_mags_raw)
            if 0.3 <= acc_med <= 3.0:
                acc_mags_g = acc_mags_raw[:]  # already in g
            else:
                # looks like m/s^2, convert to g
                acc_mags_g = [v / 9.81 for v in acc_mags_raw]

        # -----------------------------
        # Robust thresholds (window-based)
        # -----------------------------
        def robust_thresholds(vals: List[float], floor_low: float, mult_iqr: float, mult_iqr_high: float):
            if len(vals) < 6:
                return None
            sv = sorted(vals)
            med = statistics.median(sv)
            p25 = _percentile(sv, 25)
            p75 = _percentile(sv, 75)
            iqr = max(1e-6, p75 - p25)
            thr_low = max(floor_low, med + mult_iqr * iqr)
            thr_high = max(floor_low, med + mult_iqr_high * iqr)
            return (thr_low, thr_high, med, iqr)

        acc_thr = robust_thresholds(acc_mags_g, floor_low=1.10, mult_iqr=3.0, mult_iqr_high=4.0)
        gyro_thr = robust_thresholds(gyro_mags, floor_low=10.0, mult_iqr=4.0, mult_iqr_high=10.0)

        # Hard floors tuned to real driving behavior:
        # - Sudden movement in g is usually > ~1.25g
        # - Impact-like in g is usually > ~1.40g
        acc_spike_thr_g = None
        acc_impact_thr_g = None
        if acc_thr:
            acc_spike_thr_g = max(1.25, acc_thr[0])
            acc_impact_thr_g = max(1.40, acc_thr[1])

        # Gyro units can vary. Use robust thresholds + conservative floors.
        gyro_swerve_thr = None
        gyro_violent_thr = None
        if gyro_thr:
            # swerving threshold
            gyro_swerve_thr = max(60.0, gyro_thr[0])
            # violent rotation threshold
            gyro_violent_thr = max(180.0, gyro_thr[1])

        # -----------------------------
        # Risk scoring (dashboard)
        # -----------------------------
        score = 0
        reasons: List[str] = []

        # Swerving / turning (sustained, not single spike)
        if gyro_swerve_thr is not None:
            high_gyro_count = sum(1 for g in gyro_mags if g >= gyro_swerve_thr)
            if high_gyro_count >= 4:
                score += 20
                reasons.append("swerving")

        # Sudden movement / bump / impact-ish
        acc_max_g = max(acc_mags_g) if acc_mags_g else None
        if acc_max_g is not None and acc_spike_thr_g is not None and acc_impact_thr_g is not None:
            if acc_max_g >= acc_impact_thr_g:
                score += 25
                reasons.append("impact_like")
            elif acc_max_g >= acc_spike_thr_g:
                score += 15
                reasons.append("sudden_movement")

        # Speed (keep your util)
        speed_kmh = get_speed_kmh(latest_msg, last_gps)
        if speed_kmh is not None:
            if speed_kmh > 60:
                score += 30
                reasons.append("speeding")
            elif speed_kmh > 45:
                score += 10
                reasons.append("fast")

        # Heart rate (only if finger detected)
        hr_score_added = False
        heart = latest_msg.get("heart_rate") or {}
        if isinstance(heart, dict):
            if heart.get("ok") is True and heart.get("finger") is True:
                hr = _safe_float(heart.get("hr")) or 0.0
                if hr > 125:
                    score += 10
                    reasons.append("high_hr")
                    hr_score_added = True

        # Clamp
        score = min(100, max(0, score))

        # Level thresholds unchanged
        if score >= 70:
            level = "DANGEROUS"
        elif score >= 40:
            level = "RISKY"
        else:
            level = "NORMAL"

        # -----------------------------
        # ML Gating (conservative)
        # -----------------------------
        ml_gate = False
        ml_gate_reasons: List[str] = []
        ml_debug: Dict[str, Any] = {}

        # speed series from velocity only (fast + stable)
        speed_series: List[float] = []
        for msg in window_msgs:
            vel = msg.get("velocity") or {}
            if isinstance(vel, dict):
                v = _safe_float(vel.get("kmh"))
                if v is not None and 0 <= v <= 250:
                    speed_series.append(v)

        speed_start = speed_series[0] if len(speed_series) >= 2 else None
        speed_end = speed_series[-1] if len(speed_series) >= 2 else None
        speed_drop = (speed_start - speed_end) if (speed_start is not None and speed_end is not None) else None

        gyro_max = max(gyro_mags) if gyro_mags else None

        # Conservative trigger: strong IMU event + speed drop OR low end-speed
        # (tune later once you collect crash-like events)
        impact_trigger = False
        rotation_trigger = False
        if acc_max_g is not None and acc_impact_thr_g is not None:
            impact_trigger = acc_max_g >= max(1.55, acc_impact_thr_g + 0.10)

        if gyro_max is not None:
            rotation_trigger = gyro_max >= 220.0

        drop_or_stop = False
        if speed_drop is not None and speed_end is not None:
            drop_or_stop = (speed_drop >= 20.0) or (speed_end <= 10.0)

        if (impact_trigger or rotation_trigger) and drop_or_stop:
            ml_gate = True
            if impact_trigger:
                ml_gate_reasons.append("impact")
            if rotation_trigger:
                ml_gate_reasons.append("rotation")
            if speed_drop is not None and speed_drop >= 20.0:
                ml_gate_reasons.append("speed_drop")
            elif speed_end is not None and speed_end <= 10.0:
                ml_gate_reasons.append("low_speed")

        ml_debug.update(
            {
                "acc_max_g": round(acc_max_g, 3) if acc_max_g is not None else None,
                "gyro_max": round(gyro_max, 2) if gyro_max is not None else None,
                "speed_start": round(speed_start, 1) if speed_start is not None else None,
                "speed_end": round(speed_end, 1) if speed_end is not None else None,
                "speed_drop": round(speed_drop, 1) if speed_drop is not None else None,
                "acc_spike_thr_g": round(acc_spike_thr_g, 3) if acc_spike_thr_g is not None else None,
                "acc_impact_thr_g": round(acc_impact_thr_g, 3) if acc_impact_thr_g is not None else None,
                "gyro_swerve_thr": round(gyro_swerve_thr, 2) if gyro_swerve_thr is not None else None,
                "gyro_violent_thr": round(gyro_violent_thr, 2) if gyro_violent_thr is not None else None,
            }
        )

        return {
            "level": level,
            "score": score,
            "reasons": reasons,
            "speed_kmh": round(speed_kmh, 1) if speed_kmh is not None else None,
            "ml_gate": ml_gate,
            "ml_gate_reasons": ml_gate_reasons,
            "ml_gate_debug": ml_debug,
        }
