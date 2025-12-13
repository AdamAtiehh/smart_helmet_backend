# app/models/schemas.py
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional, Literal, Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator,EmailStr
from uuid import UUID



# -----------------------------
# Enums
# -----------------------------
class TripStatus(str, Enum):
    recording = "recording"
    completed = "completed"
    cancelled = "cancelled"
    
# Without an enum, someone could send "status": "potato" and it would be accepted.
# With TripStatus, Pydantic automatically validates that the field is one of those 3 valid strings.


class AlertType(str, Enum):
    crash = "crash"
    high_hr = "high_hr"
    low_hr = "low_hr"
    battery_low = "battery_low"
    fall_detected = "fall_detected"
    geo_fence = "geo_fence"
    crash_edge = "crash_edge"      # from device ML
    crash_server = "crash_server"  # from server ML
    
    
# You’ll later query or filter alerts easily, e.g.:
# SELECT * FROM alerts WHERE alert_type = 'crash_server';


class Severity(str, Enum):
    info = "info"
    warning = "warning"
    critical = "critical"


# -----------------------------
# Common sensor blocks
# -----------------------------
# -----------------------------
# Nested Data Structures
# -----------------------------
class HeartRateData(BaseModel):
    ok: bool
    ir: int
    red: int
    finger: bool
    hr: int
    spo2: int

class IMUData(BaseModel):
    ok: bool
    sleep: bool
    ax: float
    ay: float
    az: float
    gx: float
    gy: float
    gz: float

class GPSData(BaseModel):
    ok: bool
    lat: float
    lng: float
    alt: float
    sats: int
    lock: bool


# -----------------------------
# Ingest payloads (from device/app)
# -----------------------------
class TripStartIn(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")
    type: Literal["trip_start"]
    device_id: str
    ts: datetime
    ts: datetime

    @field_validator("ts", mode="before")
    @classmethod
    def parse_ts(cls, v):
        if isinstance(v, str):
            # Try custom format first, then ISO
            try:
                return datetime.strptime(v, "%d/%m/%Y %H:%M:%S")
            except ValueError:
                pass
        return v

    @field_validator("device_id")
    @classmethod
    def non_empty_device(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("device_id cannot be empty")
        return v


class TripEndIn(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")
    type: Literal["trip_end"]
    device_id: str
    ts: datetime
    ts: datetime

    @field_validator("ts", mode="before")
    @classmethod
    def parse_ts(cls, v):
        if isinstance(v, str):
            try:
                return datetime.strptime(v, "%d/%m/%Y %H:%M:%S")
            except ValueError:
                pass
        return v


class TelemetryIn(BaseModel):
    """
    Nested telemetry payload.
    """
    model_config = ConfigDict(from_attributes=True, extra="ignore")

    ts: datetime
    type: Literal["telemetry"]
    device_id: str
    helmet_on: bool
    heart_rate: HeartRateData
    imu: IMUData
    gps: GPSData
    crash_flag: bool
    trip_id: Optional[str] = None

    @field_validator("ts", mode="before")
    @classmethod
    def parse_ts(cls, v):
        if isinstance(v, str):
            try:
                return datetime.strptime(v, "%d/%m/%Y %H:%M:%S")
            except ValueError:
                pass
        return v

    @field_validator("device_id")
    @classmethod
    def non_empty_device(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("device_id cannot be empty")
        return v


class AlertIn(BaseModel):
    """
    Alert reported by device ML (edge) or any upstream process.
    Server can also emit its own alerts (see AlertOut).
    """
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    type: Literal["alert"]
    device_id: str
    ts: datetime
    trip_id: Optional[str] = None
    alert_type: AlertType
    severity: Severity
    message: str
    # lightweight snapshot (e.g., a_mag, hr, lat/lng)
    payload: Optional[dict] = None

    @field_validator("ts", mode="before")
    @classmethod
    def parse_ts(cls, v):
        if isinstance(v, str):
            try:
                return datetime.strptime(v, "%d/%m/%Y %H:%M:%S")
            except ValueError:
                pass
        return v


# -----------------------------
# Server outputs / API reads
# -----------------------------
class DeviceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    device_id: str
    user_id: Optional[str] = None
    device_serial: Optional[str] = None
    model_name: Optional[str] = None
    firmware_version: Optional[str] = None
    last_seen_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    @model_validator(mode="after")
    def convert_timezones(self):
        beirut = ZoneInfo("Asia/Beirut")
        utc = ZoneInfo("UTC")
        for field in ["last_seen_at", "created_at"]:
            val = getattr(self, field)
            if val is not None:
                if val.tzinfo is None:
                    val = val.replace(tzinfo=utc)
                setattr(self, field, val.astimezone(beirut))
        return self


class TripRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trip_id: str
    user_id: Optional[str] = None
    device_id: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    start_lat: Optional[float] = None
    start_lng: Optional[float] = None
    end_lat: Optional[float] = None
    end_lng: Optional[float] = None
    average_speed: Optional[float] = None
    max_speed: Optional[float] = None
    total_distance: Optional[float] = None
    average_heart_rate: Optional[float] = None
    max_heart_rate: Optional[float] = None
    crash_detected: Optional[bool] = None
    crash_count: Optional[int] = None
    status: Optional[TripStatus] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @model_validator(mode="after")
    def convert_timezones(self):
        beirut = ZoneInfo("Asia/Beirut")
        utc = ZoneInfo("UTC")
        for field in ["start_time", "end_time", "created_at", "updated_at"]:
            val = getattr(self, field)
            if val is not None:
                if val.tzinfo is None:
                    val = val.replace(tzinfo=utc)
                setattr(self, field, val.astimezone(beirut))
        return self


class TripDataRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    data_id: int
    trip_id: Optional[str] = None
    device_id: str
    timestamp: datetime
    
    # Nested structures
    heart_rate: Optional[HeartRateData] = None
    imu: Optional[IMUData] = None
    gps: Optional[GPSData] = None
    
    helmet_on: Optional[bool] = None # Not in DB yet, but in JSON
    crash_flag: Optional[bool] = None
    battery_pct: Optional[float] = None
    
    created_at: Optional[datetime] = None

    @model_validator(mode='before')
    @classmethod
    def map_flat_to_nested(cls, data: Any) -> Any:
        # If it's a dict (already processed)
        if isinstance(data, dict):
            return data
            
        # If it's an ORM object (TripData)
        # We need to reconstruct the nested objects from flat columns
        
        # Helper to safely get attr
        def get(attr, default=None):
            return getattr(data, attr, default)

        # Construct HeartRateData
        hr_val = get("heart_rate")
        # Note: DB only stores 'heart_rate' (int/float). 
        # The other fields (ir, red, finger, spo2) are likely lost unless stored in raw_payload.
        # For now, we'll populate what we have and use defaults/dummy for others if missing,
        # OR check raw_payload if available.
        
        # raw = get("raw_payload") or {}
        
        # Try to get full HR data from raw_payload if it exists, else partial
        hr_data = None
        if "heart_rate" in raw and isinstance(raw["heart_rate"], dict):
             hr_data = raw["heart_rate"]
        elif hr_val is not None:
            # Reconstruct partial
            hr_data = {
                "ok": True,
                "ir": 0, "red": 0, "finger": True, "spo2": 0,
                "hr": int(hr_val)
            }
            
        # Construct IMUData
        imu_data = None
        if "imu" in raw and isinstance(raw["imu"], dict):
            imu_data = raw["imu"]
        elif get("acc_x") is not None:
            imu_data = {
                "ok": True, "sleep": False,
                "ax": get("acc_x", 0.0), "ay": get("acc_y", 0.0), "az": get("acc_z", 0.0),
                "gx": get("gyro_x", 0.0), "gy": get("gyro_y", 0.0), "gz": get("gyro_z", 0.0)
            }

        # Construct GPSData
        gps_data = None
        if "gps" in raw and isinstance(raw["gps"], dict):
            gps_data = raw["gps"]
        elif get("lat") is not None:
            gps_data = {
                "ok": True,
                "lat": get("lat", 0.0), "lng": get("lng", 0.0),
                "alt": 0.0, "sats": 0, "lock": True
            }

        # Return a dict that matches the schema
        return {
            "data_id": get("data_id"),
            "trip_id": get("trip_id"),
            "device_id": get("device_id"),
            "timestamp": get("timestamp"),
            "heart_rate": hr_data,
            "imu": imu_data,
            "gps": gps_data,
            "helmet_on": raw.get("helmet_on", False),
            "crash_flag": get("crash_flag"),
            "created_at": get("created_at"),
        }

    @model_validator(mode="after")
    def convert_timezones(self):
        beirut = ZoneInfo("Asia/Beirut")
        utc = ZoneInfo("UTC")
        for field in ["timestamp", "created_at"]:
            val = getattr(self, field)
            if val is not None:
                if val.tzinfo is None:
                    val = val.replace(tzinfo=utc)
                setattr(self, field, val.astimezone(beirut))
        return self




# --- Schemas ---
class TripSummaryOut(BaseModel):
    trip_id: str
    device_id: Optional[str]
    start_time: datetime
    end_time: Optional[datetime]
    total_distance: Optional[float] = None
    average_speed: Optional[float] = None
    status: str

    @model_validator(mode="after")
    def convert_timezones(self):
        beirut = ZoneInfo("Asia/Beirut")
        utc = ZoneInfo("UTC")
        for field in ["start_time", "end_time"]:
            val = getattr(self, field)
            if val is not None:
                if val.tzinfo is None:
                    val = val.replace(tzinfo=utc)
                setattr(self, field, val.astimezone(beirut))
        return self

class TripDetailOut(TripSummaryOut):
    max_speed: Optional[float] = None
    average_heart_rate: Optional[float] = None
    start_lat: Optional[float] = None
    start_lng: Optional[float] = None
    end_lat: Optional[float] = None
    end_lng: Optional[float] = None
    max_heart_rate: Optional[float] = None
    crash_detected: Optional[bool] = None

class RoutePoint(BaseModel):
    lat: float
    lng: float
    ts: datetime
    speed: Optional[float]


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    alert_id: Optional[str] = None
    user_id: Optional[str] = None
    trip_id: Optional[str] = None
    device_id: str
    ts: datetime
    alert_type: AlertType
    severity: Severity
    message: str
    payload_json: Optional[dict] = None
    created_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None

    @model_validator(mode="after")
    def convert_timezones(self):
        beirut = ZoneInfo("Asia/Beirut")
        utc = ZoneInfo("UTC")
        for field in ["ts", "created_at", "resolved_at"]:
            val = getattr(self, field)
            if val is not None:
                if val.tzinfo is None:
                    val = val.replace(tzinfo=utc)
                setattr(self, field, val.astimezone(beirut))
        return self


# -----------------------------
# API Request/Response Models
# -----------------------------

class DeviceCreate(BaseModel):
    device_id: str
    model_name: Optional[str] = "Smart Helmet v1"
    device_serial: Optional[str] = None

class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    user_id: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    phone_number: Optional[str] = None
    created_at: Optional[datetime] = None

    @model_validator(mode="after")
    def convert_timezones(self):
        beirut = ZoneInfo("Asia/Beirut")
        utc = ZoneInfo("UTC")
        if self.created_at is not None:
            if self.created_at.tzinfo is None:
                self.created_at = self.created_at.replace(tzinfo=utc)
            self.created_at = self.created_at.astimezone(beirut)
        return self

class UserUpdate(BaseModel):
    display_name: Optional[str] = None
    email: Optional[EmailStr] = None
    phone_number: Optional[str] = None
    
class RoutePoint(BaseModel):
    lat: float
    lng: float
    ts: datetime
    speed: Optional[float] = None

    @model_validator(mode="after")
    def convert_timezones(self):
        beirut = ZoneInfo("Asia/Beirut")
        utc = ZoneInfo("UTC")
        if self.ts is not None:
            if self.ts.tzinfo is None:
                self.ts = self.ts.replace(tzinfo=utc)
            self.ts = self.ts.astimezone(beirut)
        return self



class AuthUser(BaseModel):
    user_id: UUID
    firebase_uid: str
    email: Optional[EmailStr] = None
    display_name: Optional[str] = None
    phone_number: Optional[str] = None