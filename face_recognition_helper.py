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

    def align_face_landmarks(self, image_rgb, face_data):
        """
        Landmark-based 5-point similarity alignment to 112x112.
        Matches ESP32 face_recognition_tool::align_face() behaviour.
        YuNet outputs 5 keypoints: [left_eye, right_eye, nose, left_mouth, right_mouth]
        each as (x, y) packed in face_data[4:14].
        """
        # Standard template for 112x112 (ArcFace / MobileFaceNet canonical positions)
        REFERENCE_LANDMARKS = np.array([
            [38.2946, 51.6963],  # left  eye
            [73.5318, 51.5014],  # right eye
            [56.0252, 71.7366],  # nose tip
            [41.5493, 92.3655],  # left  mouth corner
            [70.7299, 92.2041],  # right mouth corner
        ], dtype=np.float32)

        try:
            # YuNet face_data layout: [x, y, w, h, conf, lm0x, lm0y, lm1x, lm1y, ... lm4x, lm4y]
            kps = face_data[4:14].reshape(5, 2).astype(np.float32)
        except Exception:
            # Fallback to simple crop if landmarks are not available
            return self.crop_face(image_rgb, face_data)

        # Estimate affine transform from 5 keypoints → reference positions
        try:
            tform, _ = cv2.estimateAffinePartial2D(
                kps, REFERENCE_LANDMARKS,
                method=cv2.LMEDS
            )
            if tform is None:
                return self.crop_face(image_rgb, face_data)
            aligned = cv2.warpAffine(image_rgb, tform, (112, 112), flags=cv2.INTER_LINEAR)
            return aligned
        except Exception:
            return self.crop_face(image_rgb, face_data)

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
            
            # CRITICAL FIX: Convert BGR to RGB to match ESP32 color space
            # ESP32 uses RGB format, OpenCV uses BGR by default
            # This mismatch was causing embeddings to not match even for the same person
            image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
            
            # 2. Landmark-aligned crop to 112x112 (matches ESP32 face_recognition_tool::align_face)
            face_align = self.align_face_landmarks(image_rgb, face)
            
            # 3. Preprocess for MobileFaceNet
            # Normalization: (x - 127.5) / 128.0 — matches ESP32 MobileFaceNet preprocessing
            blob = cv2.resize(face_align, (112, 112))
            blob = blob.astype(np.float32)
            blob = (blob - 127.5) / 128.0
            blob = np.transpose(blob, (2, 0, 1)) # HWC to CHW
            blob = np.expand_dims(blob, axis=0)
            
            # 4. Run Inference
            input_name = self.recognizer.get_inputs()[0].name
            embedding = self.recognizer.run(None, {input_name: blob})[0]
            
            # 5. L2-Normalize embedding to match ESP32 behavior
            norm = np.linalg.norm(embedding)
            if norm > 1e-6:
                embedding = embedding / norm
                
            return embedding.flatten().tolist(), None
            
        except Exception as e:
            return None, f"Extraction error: {str(e)}"

    def crop_face(self, image, face_data):
        """Simple bbox crop with margin (fallback when landmarks unavailable)."""
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
