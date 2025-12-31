import os
import joblib
import numpy as np
from typing import Dict, Any, List, Optional

# Singleton model cache
_MODEL = None
_MODEL_PATH = "app/ml/baseline_model.pkl"

def _load_model():
    global _MODEL
    if _MODEL:
        return _MODEL
    
    if not os.path.exists(_MODEL_PATH):
        print(f"[Predictor] Warning: Model file not found at {_MODEL_PATH}")
        return None
    
    try:
        _MODEL = joblib.load(_MODEL_PATH)
        print(f"[Predictor] Model loaded from {_MODEL_PATH}")
    except Exception as e:
        print(f"[Predictor] Error loading model: {e}")
    
    return _MODEL

def extract_features(window_msgs: List[Dict[str, Any]]) -> np.ndarray:
    """
    Convert a list of telemetry messages (trip_data dicts) into a feature vector.
    Expected features:
    - max/mean/std of accel magnitude
    - max/mean/std of gyro magnitude
    - delta HR
    - mean HR
    """
    if not window_msgs:
        return np.zeros((1, 8))

    # Parse into arrays with safe defaults
    acc_x = np.array([m.get('imu', {}).get('ax', 0.0) for m in window_msgs])
    acc_y = np.array([m.get('imu', {}).get('ay', 0.0) for m in window_msgs])
    acc_z = np.array([m.get('imu', {}).get('az', 0.0) for m in window_msgs])
    
    gyro_x = np.array([m.get('imu', {}).get('gx', 0.0) for m in window_msgs])
    gyro_y = np.array([m.get('imu', {}).get('gy', 0.0) for m in window_msgs])
    gyro_z = np.array([m.get('imu', {}).get('gz', 0.0) for m in window_msgs])
    
    hr = np.array([m.get('heart_rate', {}).get('hr', 0.0) for m in window_msgs])

    # Magnitudes
    acc_mag = np.sqrt(acc_x**2 + acc_y**2 + acc_z**2)
    gyro_mag = np.sqrt(gyro_x**2 + gyro_y**2 + gyro_z**2)
    
    features = [
        np.max(acc_mag),
        np.mean(acc_mag),
        np.std(acc_mag),
        np.max(gyro_mag),
        np.mean(gyro_mag),
        np.std(gyro_mag),
        float(hr[-1] - hr[0]), # delta HR
        np.mean(hr)
    ]
    
    return np.array([features]) # Shape (1, 8)

def predict_crash(window_msgs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Main entry point for crash prediction.
    """
    model = _load_model()
    if not model:
        return {
            "label": "unknown",
            "prob": 0.0,
            "model": "baseline_v1"
        }
        
    try:
        X = extract_features(window_msgs)
        probs = model.predict_proba(X)[0] # e.g. [0.1, 0.9] for [no_crash, crash]
        crash_prob = float(probs[1]) 
        
        label = "crash" if crash_prob > 0.5 else "no_crash"
        
        return {
            "label": label,
            "prob": crash_prob,
            "model": "baseline_v1"
        }
    except Exception as e:
        print(f"[Predictor] Inference error: {e}")
        return {
            "label": "error",
            "prob": 0.0,
            "model": "baseline_v1"
        }
