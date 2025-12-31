import numpy as np
import pandas as pd
import joblib
import os
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report

# Configuration
WINDOW_SIZE = 50  # Total samples (e.g. 30 pre + 20 post)
NUM_SAMPLES_NORMAL = 500
NUM_SAMPLES_CRASH = 100
MODEL_PATH = "app/ml/baseline_model.pkl"

def generate_window(is_crash: bool, length=WINDOW_SIZE):
    """
    Generate a single window of telemetry features.
    Returns: dict of raw arrays for accel/gyro/hr
    """
    # Time vector (just for noise generation)
    t = np.linspace(0, 5, length)
    
    # Base noise
    ax = np.random.normal(0, 0.1, length)
    ay = np.random.normal(0, 0.1, length)
    az = np.random.normal(9.8, 0.2, length)  # Gravity
    
    gx = np.random.normal(0, 0.05, length)
    gy = np.random.normal(0, 0.05, length)
    gz = np.random.normal(0, 0.05, length)
    
    hr = np.random.normal(80, 5, length)
    
    if is_crash:
        # Crash event happens roughly in the middle
        impact_idx = length // 2
        
        # Spike in accel
        spike_mag = np.random.uniform(5, 15)
        ax[impact_idx] += spike_mag * np.random.choice([-1, 1])
        ay[impact_idx] += spike_mag * np.random.choice([-1, 1])
        
        # Disorientation (gravity vector shifts significantly after impact)
        # We model this by random rotations or just chaos for a few samples
        for i in range(impact_idx, min(length, impact_idx + 10)):
            ax[i] += np.random.normal(0, 5)
            ay[i] += np.random.normal(0, 5)
            az[i] += np.random.normal(0, 5)
            
            gx[i] += np.random.normal(0, 3)
            gy[i] += np.random.normal(0, 3)
            gz[i] += np.random.normal(0, 3)
            
        # HR might spike slightly after
        hr[impact_idx:] += np.linspace(0, 20, length - impact_idx)

    # Dictionary representation similar to what we'd get from a list of objects
    return {
        "acc_x": ax, "acc_y": ay, "acc_z": az,
        "gyro_x": gx, "gyro_y": gy, "gyro_z": gz,
        "hr": hr
    }

def extract_features(window_data):
    """
    Compute aggregate features for the window.
    """
    # Calculate magnitudes
    acc_mag = np.sqrt(window_data["acc_x"]**2 + window_data["acc_y"]**2 + window_data["acc_z"]**2)
    gyro_mag = np.sqrt(window_data["gyro_x"]**2 + window_data["gyro_y"]**2 + window_data["gyro_z"]**2)
    
    features = [
        np.max(acc_mag),
        np.mean(acc_mag),
        np.std(acc_mag),
        np.max(gyro_mag),
        np.mean(gyro_mag),
        np.std(gyro_mag),
        window_data["hr"][-1] - window_data["hr"][0], # delta HR
        np.mean(window_data["hr"])
    ]
    return features

def main():
    print("Generating synthetic data...")
    X = []
    y = []
    
    # 1. Generate Normal Windows
    for _ in range(NUM_SAMPLES_NORMAL):
        w = generate_window(is_crash=False)
        f = extract_features(w)
        X.append(f)
        y.append(0) # 'no_crash'
        
    # 2. Generate Crash Windows
    for _ in range(NUM_SAMPLES_CRASH):
        w = generate_window(is_crash=True)
        f = extract_features(w)
        X.append(f)
        y.append(1) # 'crash'
        
    X = np.array(X)
    y = np.array(y)
    
    print(f"Dataset shape: {X.shape}")
    
    # Split
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    
    # Train
    print("Training RandomForestClassifier...")
    clf = RandomForestClassifier(n_estimators=50, max_depth=5, random_state=42)
    clf.fit(X_train, y_train)
    
    # Eval
    y_pred = clf.predict(X_test)
    print("Evaluation:")
    print(classification_report(y_test, y_pred, target_names=["no_crash", "crash"]))
    
    # Save
    os.makedirs("app/ml", exist_ok=True)
    joblib.dump(clf, MODEL_PATH)
    print(f"Model saved to {MODEL_PATH}")

if __name__ == "__main__":
    main()
