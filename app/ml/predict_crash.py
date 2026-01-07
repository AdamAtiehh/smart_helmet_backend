import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import joblib



BASE_DIR = Path(__file__).resolve().parent  # app/ml/

PIPELINE_PATH = BASE_DIR / "crash_iforest_pipeline.joblib"
CFG_PATH = BASE_DIR / "crash_model_config.json"

class CrashModel:
    """
    Loads:
      - crash_iforest_pipeline.joblib  (StandardScaler + IsolationForest)
      - crash_model_config.json        (feature order, window size, score_threshold)
    """

    def __init__(self, pipeline_path: str, config_path: str):
        self.pipeline_path = pipeline_path
        self.config_path = config_path
        self.pipeline = None
        self.cfg = None

    def load(self):
        if self.pipeline is None:
            self.pipeline = joblib.load(self.pipeline_path)
        if self.cfg is None:
            with open(self.config_path, "r") as f:
                self.cfg = json.load(f)

    @staticmethod
    def _f(x: Any) -> Optional[float]:
        try:
            if x is None:
                return None
            return float(x)
        except Exception:
            return None

    @staticmethod
    def _mag3(a: float, b: float, c: float) -> float:
        return math.sqrt(a * a + b * b + c * c)

    def _extract_window_series(self, window_msgs: List[Dict[str, Any]]):
        """
        Build raw series arrays from telemetry dicts.
        Expects each msg dict similar to payload.model_dump():
          msg["imu"]["ax"], msg["velocity"]["kmh"], etc.
        """
        speeds = []
        acc_mag = []
        gyro_mag = []

        for msg in window_msgs:
            imu = msg.get("imu") or {}
            vel = msg.get("velocity") or {}

            # IMU validity gating
            imu_ok = True
            if isinstance(imu, dict):
                if imu.get("ok") is False:
                    imu_ok = False
                if imu.get("sleep") is True:
                    imu_ok = False
            else:
                imu_ok = False

            # speed (velocity.kmh) — prefer this in backend
            v = None
            if isinstance(vel, dict):
                v = self._f(vel.get("kmh"))
            speeds.append(v)

            # IMU magnitudes
            if imu_ok and isinstance(imu, dict):
                ax = self._f(imu.get("ax")) or 0.0
                ay = self._f(imu.get("ay")) or 0.0
                az = self._f(imu.get("az")) or 0.0
                gx = self._f(imu.get("gx")) or 0.0
                gy = self._f(imu.get("gy")) or 0.0
                gz = self._f(imu.get("gz")) or 0.0

                acc_mag.append(self._mag3(ax, ay, az))
                gyro_mag.append(self._mag3(gx, gy, gz))
            else:
                acc_mag.append(None)
                gyro_mag.append(None)

        return speeds, acc_mag, gyro_mag

    @staticmethod
    def _nan_stats(arr: List[Optional[float]]):
        x = np.array([v if v is not None else np.nan for v in arr], dtype=float)
        return x

    def featurize(self, window_msgs: List[Dict[str, Any]]) -> Optional[Dict[str, float]]:
        """
        Builds the same features you trained on:
          acc_mean, acc_std, acc_max, acc_min,
          jerk_mean, jerk_max,
          gyro_mean, gyro_std, gyro_max,
          speed_mean, speed_max, speed_min, speed_delta
        """
        self.load()
        win = int(self.cfg["window_samples"])

        if len(window_msgs) < win:
            return None

        # Use last win samples
        wmsgs = window_msgs[-win:]

        speeds, acc_mag, gyro_mag = self._extract_window_series(wmsgs)

        acc = self._nan_stats(acc_mag)
        gyro = self._nan_stats(gyro_mag)
        spd = self._nan_stats(speeds)

        # Need enough valid points (otherwise features become NaN)
        if np.isnan(acc).all() or np.isnan(gyro).all() or np.isnan(spd).all():
            return None

        # jerk = abs(diff(acc_mag))
        jerk = np.abs(np.diff(acc))
        # diff produces length win-1; pad to win with nan at start for stats consistency
        jerk = np.insert(jerk, 0, np.nan)

        feats = {
            "acc_mean": float(np.nanmean(acc)),
            "acc_std": float(np.nanstd(acc)),
            "acc_max": float(np.nanmax(acc)),
            "acc_min": float(np.nanmin(acc)),

            "jerk_mean": float(np.nanmean(jerk)),
            "jerk_max": float(np.nanmax(jerk)),

            "gyro_mean": float(np.nanmean(gyro)),
            "gyro_std": float(np.nanstd(gyro)),
            "gyro_max": float(np.nanmax(gyro)),

            "speed_mean": float(np.nanmean(spd)),
            "speed_max": float(np.nanmax(spd)),
            "speed_min": float(np.nanmin(spd)),
            # speed_delta = speed_end - speed_start (same as training script)
            "speed_delta": float(spd[-1] - spd[0]) if (not np.isnan(spd[-1]) and not np.isnan(spd[0])) else 0.0,
        }
        return feats

    def predict(self, window_msgs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Returns dict compatible with your persist_worker usage:
          - is_anomaly
          - prob (we’ll map anomaly score to a 0..1 “confidence-ish” number)
          - score (raw anomaly_score)
          - threshold (score_threshold)
          - features (dict)
          - model (string)
        """
        self.load()
        feats = self.featurize(window_msgs)
        if feats is None:
            return {"error": "insufficient_or_invalid_window"}

        feature_cols = self.cfg["feature_cols"]
        X = np.array([[feats[c] for c in feature_cols]], dtype=float)

        # decision_function: higher=normal, lower=anomalous
        score = float(self.pipeline.decision_function(X)[0])
        threshold = float(self.cfg["score_threshold"])
        is_anomaly = bool(score < threshold)

        # “prob” mapping: not a true probability, but useful for UI/logging.
        # We map distance below threshold into 0..1.
        margin = threshold - score  # positive when anomalous
        prob = float(1.0 / (1.0 + math.exp(-5.0 * margin)))  # sigmoid

        return {
            "model": "iforest_scaled",
            "features": feats,
            "score": score,
            "threshold": threshold,
            "is_anomaly": is_anomaly,
            "prob": prob,
        }


_model_singleton = None

def predict_crash(full_window):
    global _model_singleton
    if _model_singleton is None:
        _model_singleton = CrashModel(
            pipeline_path=str(PIPELINE_PATH),
            config_path=str(CFG_PATH),
        )
        _model_singleton.load()
    return _model_singleton.predict(full_window)
