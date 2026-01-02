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

    @field_validator("ts", mode="before")
    @classmethod
    def parse_ts(cls, v):
        if isinstance(v, str):
            try:
                return datetime.strptime(v, "%d/%m/%Y %H:%M:%S")
            except ValueError:
                pass
        return v


class VelocityData(BaseModel):
    kmh: Optional[float] = None

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
    velocity: Optional[VelocityData] = None
    crash_flag: Optional[bool] = None
    trip_id: Optional[str] = None

    @field_validator("velocity")
    @classmethod
    def validate_velocity(cls, v: Optional[VelocityData]) -> Optional[VelocityData]:
        if v and v.kmh is not None:
            # Ignore negative or absurdly high speed
            if v.kmh < 0 or v.kmh > 250:
                v.kmh = None
        return v

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

    heart_rate: Optional[HeartRateData] = None
    imu: Optional[IMUData] = None
    gps: Optional[GPSData] = None

    helmet_on: Optional[bool] = None
    crash_flag: Optional[bool] = None
    battery_pct: Optional[float] = None
    created_at: Optional[datetime] = None

    @model_validator(mode="before")
    @classmethod
    def map_flat_to_nested(cls, data: Any) -> Any:
        # If it's already a dict, just pass through
        if isinstance(data, dict):
            return data

        def get(attr, default=None):
            return getattr(data, attr, default)

        # Heart rate (DB stores only the HR number)
        hr_val = get("heart_rate")
        hr_data = None
        if hr_val is not None:
            hr_data = {
                "ok": True,
                "ir": 0,
                "red": 0,
                "finger": True,
                "hr": int(hr_val),
                "spo2": 0,
            }

        # IMU (DB stores flat acc/gyro)
        imu_data = None
        if get("acc_x") is not None:
            imu_data = {
                "ok": True,
                "sleep": False,
                "ax": get("acc_x", 0.0),
                "ay": get("acc_y", 0.0),
                "az": get("acc_z", 0.0),
                "gx": get("gyro_x", 0.0),
                "gy": get("gyro_y", 0.0),
                "gz": get("gyro_z", 0.0),
            }

        # GPS (DB stores lat/lng only)
        gps_data = None
        if get("lat") is not None and get("lng") is not None:
            gps_data = {
                "ok": True,
                "lat": get("lat", 0.0),
                "lng": get("lng", 0.0),
                "alt": 0.0,
                "sats": 0,
                "lock": True,
            }

        return {
            "data_id": get("data_id"),
            "trip_id": get("trip_id"),
            "device_id": get("device_id"),
            "timestamp": get("timestamp"),
            "heart_rate": hr_data,
            "imu": imu_data,
            "gps": gps_data,
            # since you don't store helmet_on in DB, default to None or False
            "helmet_on": None,  # live-only, not persisted
            "crash_flag": get("crash_flag"),
            "battery_pct": get("battery_pct"),
            "created_at": get("created_at"),
        }


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


class DailyHistoryOut(BaseModel):
    date: str
    average_heart_rate: float
    max_heart_rate: float
    average_speed: float
    total_distance: float
    total_trips: int