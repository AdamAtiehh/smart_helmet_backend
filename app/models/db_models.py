# app/models/db_models.py
from __future__ import annotations

import uuid
from datetime import datetime
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    Enum,
    func,
    UniqueConstraint,
    Index
)
from sqlalchemy.dialects.mysql import BIGINT
from sqlalchemy.orm import DeclarativeBase, relationship


# Base class for all ORM models
class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------
# USERS  (linked to Firebase Auth)
# --------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    user_id = Column(String(128), primary_key=True, default=lambda: str(uuid.uuid4()))
    firebase_uid = Column(String(255), unique=True, nullable=True, index=True)
    email = Column(String(255), unique=True, nullable=True)
    display_name = Column(String(255), nullable=True)
    phone_number = Column(String(32), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    # Relationship (optional)
    devices = relationship("UserDevice", back_populates="user")


# --------------------------------------------------------------------
# USER ↔ DEVICE ownership map
# --------------------------------------------------------------------
class UserDevice(Base):
    __tablename__ = "user_devices"
    __table_args__ = (UniqueConstraint("user_id", "device_id", name="uq_user_device"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(128), ForeignKey("users.user_id", ondelete="CASCADE"))
    device_id = Column(String(64), ForeignKey("devices.device_id", ondelete="CASCADE"))
    role = Column(String(32), default="owner")  # owner / viewer / editor
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="devices")
    device = relationship("Device", back_populates="users")


# --------------------------------------------------------------------
# DEVICES (Helmets / Raspberry Pis)
# --------------------------------------------------------------------
class Device(Base):
    __tablename__ = "devices"

    device_id = Column(String(64), primary_key=True)
    user_id = Column(String(128), ForeignKey("users.user_id"), nullable=True)
    device_serial = Column(String(128), nullable=True)
    model_name = Column(String(128), nullable=True)
    firmware_version = Column(String(64), nullable=True)
    last_seen_at = Column(DateTime, index=True)
    created_at = Column(DateTime, server_default=func.now())

    users = relationship("UserDevice", back_populates="device")
    trips = relationship("Trip", back_populates="device")


# --------------------------------------------------------------------
# TRIPS (one ride/session)
# --------------------------------------------------------------------
class Trip(Base):
    __tablename__ = "trips"
    # ensures only one active (recording) trip per device
    __table_args__ = (
        Index("uq_one_active_trip_per_device", "active_key", unique=True),
    )

    # NEW
    active_key = Column(String(64), nullable=True, index=True)
    trip_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(128), ForeignKey("users.user_id"), nullable=True)
    device_id = Column(String(64), ForeignKey("devices.device_id", ondelete="SET NULL"))

    start_time = Column(DateTime)
    end_time = Column(DateTime, nullable=True)

    start_lat = Column(Float, nullable=True)
    start_lng = Column(Float, nullable=True)
    end_lat = Column(Float, nullable=True)
    end_lng = Column(Float, nullable=True)

    average_speed = Column(Float, nullable=True)
    max_speed = Column(Float, nullable=True)
    total_distance = Column(Float, nullable=True)
    average_heart_rate = Column(Float, nullable=True)
    max_heart_rate = Column(Float, nullable=True)

    crash_detected = Column(Boolean, default=False)
    crash_count = Column(Integer, default=0)
    status = Column(String(32), default="recording")  # recording / completed / cancelled

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    device = relationship("Device", back_populates="trips")
    trip_data = relationship("TripData", back_populates="trip")
    alerts = relationship("Alert", back_populates="trip")


# --------------------------------------------------------------------
# TRIP DATA (time-series telemetry)
# --------------------------------------------------------------------
class TripData(Base):
    __tablename__ = "trip_data"
    __table_args__ = (
        Index("idx_trip_device_time", "trip_id", "device_id", "timestamp"),
    )

    data_id = Column(Integer, primary_key=True, autoincrement=True)
    trip_id = Column(String(36), ForeignKey("trips.trip_id", ondelete="SET NULL"), index=True, nullable=True)
    device_id = Column(String(64), ForeignKey("devices.device_id", ondelete="SET NULL"), index=True)

    timestamp = Column(DateTime, index=True)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    speed_kmh = Column(Float, nullable=True)
    # speed = Column(Float, nullable=True)
    # accuracy = Column(Float, nullable=True)

    acc_x = Column(Float)
    acc_y = Column(Float)
    acc_z = Column(Float)
    gyro_x = Column(Float)
    gyro_y = Column(Float)
    gyro_z = Column(Float)

    heart_rate = Column(Float, nullable=True)
    # impact_g = Column(Float, nullable=True)
    # battery_pct = Column(Float, nullable=True)
    crash_flag = Column(Boolean, default=False)

    # raw_payload = Column(JSON, nullable=True)

    created_at = Column(DateTime, server_default=func.now())

    trip = relationship("Trip", back_populates="trip_data")


# --------------------------------------------------------------------
# ALERTS (detected events)
# --------------------------------------------------------------------
class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        Index("idx_alert_device_time", "device_id", "ts"),
    )

    alert_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(128), ForeignKey("users.user_id"), nullable=True)
    trip_id = Column(String(36), ForeignKey("trips.trip_id", ondelete="SET NULL"), nullable=True)
    device_id = Column(String(64), ForeignKey("devices.device_id", ondelete="SET NULL"), index=True)

    ts = Column(DateTime, index=True)
    alert_type = Column(String(64))
    severity = Column(String(32))  # info, warning, critical
    message = Column(Text)
    payload_json = Column(JSON)
    created_at = Column(DateTime, server_default=func.now())
    resolved_at = Column(DateTime, nullable=True)
    resolved_by = Column(String(128), nullable=True)

    trip = relationship("Trip", back_populates="alerts")


# --------------------------------------------------------------------
# PREDICTIONS (ML outputs)
# --------------------------------------------------------------------
class Prediction(Base):
    __tablename__ = "predictions"
    __table_args__ = (
        Index("idx_pred_trip_time", "trip_id", "ts"),
    )

    prediction_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    trip_id = Column(String(36), ForeignKey("trips.trip_id", ondelete="CASCADE"), nullable=True)
    device_id = Column(String(64), ForeignKey("devices.device_id", ondelete="CASCADE"), index=True)
    
    ts = Column(DateTime, default=func.now())
    model_name = Column(String(64))  # e.g. "baseline_v1"
    label = Column(String(32))       # "crash", "no_crash"
    score = Column(Float)            # 0.0 - 1.0 probability
    
    meta_json = Column(JSON, nullable=True) # features, etc.
    
    created_at = Column(DateTime, server_default=func.now())

