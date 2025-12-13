import onnx
import numpy as np
from sklearn.linear_model import LogisticRegression
from skl2onnx import convert_sklearn
from skl2onnx.common.data_types import FloatTensorType

# Create a simple dummy model
# We'll assume the input is a flattened vector of telemetry features
# features: [acc_x, acc_y, acc_z, gyro_x, gyro_y, gyro_z, heart_rate] -> 7 floats
X = np.array([[0.0] * 7, [1.0] * 7], dtype=np.float32)
y = np.array([0, 1], dtype=np.int64)

clf = LogisticRegression()
clf.fit(X, y)

# Convert to ONNX
initial_type = [('float_input', FloatTensorType([None, 7]))]
onnx_model = convert_sklearn(clf, initial_types=initial_type)

with open("app/ml/model.onnx", "wb") as f:
    f.write(onnx_model.SerializeToString())

print("✅ valid dummy model.onnx created")
