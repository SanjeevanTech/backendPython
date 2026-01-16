#!/usr/bin/env python3
"""
Face Recognition Helper (ESP32-Compatible Version)
Uses ONNX Runtime to run MobileFaceNet, matching the ESP32 hardware model.
Removes dependency on dlib and face_recognition.
"""

import base64
import io
import os
import cv2
import numpy as np
from PIL import Image
from datetime import datetime
from pathlib import Path

# Try to import onnxruntime
try:
    import onnxruntime as ort
    ONNX_AVAILABLE = True
except ImportError:
    ONNX_AVAILABLE = False
    print("[WARN] onnxruntime not installed - Using MOCK mode")

# Paths to models (should be in the same directory)
MODEL_DIR = Path(__file__).parent
DETECTOR_PATH = str(MODEL_DIR / "yunet.onnx")
RECOGNITION_PATH = str(MODEL_DIR / "mobilefacenet.onnx")

class FaceEngine:
    def __init__(self):
        self.detector = None
        self.recognizer = None
        self.initialized = False
        
        if ONNX_AVAILABLE:
            try:
                # Initialize YuNet detector
                # YuNet expects specific input sizes, but we'll use a dynamic approach
                if os.path.exists(DETECTOR_PATH):
                    self.detector = cv2.FaceDetectorYN.create(
                        DETECTOR_PATH, "", (320, 320), 0.9, 0.3, 5000
                    )
                
                # Initialize MobileFaceNet recognizer
                if os.path.exists(RECOGNITION_PATH):
                    self.recognizer = ort.InferenceSession(RECOGNITION_PATH)
                
                if self.detector and self.recognizer:
                    self.initialized = True
                    print(f"[OK] ESP32-Compatible Face Engine initialized (ONNX)")
                else:
                    if not os.path.exists(DETECTOR_PATH): print(f"[ERR] Detector missing: {DETECTOR_PATH}")
                    if not os.path.exists(RECOGNITION_PATH): print(f"[ERR] Recognizer missing: {RECOGNITION_PATH}")
            except Exception as e:
                print(f"[ERR] Failed to initialize ONNX engine: {e}")

    def extract_embedding(self, image_bgr):
        if not self.initialized:
            return None, "Engine not initialized"
        
        try:
            h, w = image_bgr.shape[:2]
            self.detector.setInputSize((w, h))
            
            # 1. Detect faces
            _, faces = self.detector.detect(image_bgr)
            
            if faces is None or len(faces) == 0:
                return None, "No face detected"
            
            # Use the first face (the most confident one)
            face = faces[0]
            
            # 2. Align and crop to 112x112 (Standard for MobileFaceNet/ESP32)
            # ESP32 models like MobileFaceNet expect 112x112 input
            face_align = self.crop_face(image_bgr, face)
            
            # 3. Preprocess for MobileFaceNet
            # Normalization typically: (x - 127.5) / 128.0
            blob = cv2.resize(face_align, (112, 112))
            blob = blob.astype(np.float32)
            blob = (blob - 127.5) / 128.0
            blob = np.transpose(blob, (2, 0, 1)) # HWC to CHW
            blob = np.expand_dims(blob, axis=0)
            
            # 4. Run Inference
            input_name = self.recognizer.get_inputs()[0].name
            embedding = self.recognizer.run(None, {input_name: blob})[0]
            
            # Normalize embedding (L2 Norm) to match ESP32 behavior
            norm = np.linalg.norm(embedding)
            if norm > 1e-6:
                embedding = embedding / norm
                
            return embedding.flatten().tolist(), None
            
        except Exception as e:
            return None, f"Extraction error: {str(e)}"

    def crop_face(self, image, face_data):
        """Simple crop based on YuNet bbox. In production, use landmarks for alignment."""
        x, y, w, h = face_data[:4].astype(int)
        # Add some margin
        margin = 0.2
        x1 = max(0, int(x - w * margin))
        y1 = max(0, int(y - h * margin))
        x2 = min(image.shape[1], int(x + w * (1 + margin)))
        y2 = min(image.shape[0], int(y + h * (1 + margin)))
        
        return image[y1:y2, x1:x2]

# Singleton instance
engine = FaceEngine()

def extract_face_embedding_from_base64(image_data_base64, draw_boxes=True):
    """
    ESP32-COMPATIBLE VERSION
    """
    try:
        # Decode base64
        if ',' in image_data_base64:
            image_data_base64 = image_data_base64.split(',')[1]
        
        image_bytes = base64.b64decode(image_data_base64)
        nparr = np.frombuffer(image_bytes, np.uint8)
        img_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        if img_bgr is None:
            return {'success': False, 'message': 'Invalid image quality'}

        embedding, error = engine.extract_embedding(img_bgr)
        
        if embedding:
            return {
                'success': True,
                'face_embedding': embedding,
                'embedding_size': len(embedding),
                'num_faces': 1,
                'message': 'Successfully extracted ESP32-compatible embedding',
                'is_mock': False
            }
        else:
            return {
                'success': False,
                'message': error or 'Could not extract embedding',
                'face_embedding': [],
                'is_mock': False
            }
            
    except Exception as e:
        return {'success': False, 'message': f'System error: {str(e)}'}

def test_face_recognition():
    print("="*60)
    print("ESP32-Compatible Face Engine Test")
    print("="*60)
    print(f"ONNX Runtime: {'OK' if ONNX_AVAILABLE else 'MISSING'}")
    print(f"Detector File: {'OK' if os.path.exists(DETECTOR_PATH) else 'MISSING'}")
    print(f"Recognizer File: {'OK' if os.path.exists(RECOGNITION_PATH) else 'MISSING'}")
    print("="*60)

if __name__ == '__main__':
    test_face_recognition()
