#!/usr/bin/env python3
"""
Face Recognition Helper
Handles face detection and embedding extraction from images
with proper image capture and storage workflow
"""

import base64
import io
import os
import numpy as np
from PIL import Image
from datetime import datetime
from pathlib import Path

# Try to import face_recognition library
try:
    import face_recognition
    FACE_RECOGNITION_AVAILABLE = True
    print("✅ face_recognition library loaded successfully")
except ImportError:
    FACE_RECOGNITION_AVAILABLE = False
    print("⚠️ face_recognition library not installed - using MOCK mode")
    print("   Install with: pip install face-recognition")

# Face recognition memory-optimized processing (No disk storage)

def extract_face_embedding_from_base64(image_data_base64, draw_boxes=True):
    """
    Extract face embedding from base64 encoded image (Memory only)
    
    Args:
        image_data_base64: Base64 encoded image string
        draw_boxes: If True, draw green bounding boxes around detected faces
    
    Returns:
        dict: {
            'success': bool,
            'face_embedding': list,
            'embedding_size': int,
            'num_faces': int,
            'face_locations': list,
            'image_with_boxes': str (base64 or none),
            'message': str,
            'is_mock': bool
        }
    """
    try:
        # STEP 1: Process the image in memory
        # Remove data URI prefix if present
        if ',' in image_data_base64:
            image_data_base64 = image_data_base64.split(',')[1]
        
        # Decode base64 to image
        image_bytes = base64.b64decode(image_data_base64)
        image = Image.open(io.BytesIO(image_bytes))
        
        # Convert to RGB if needed
        if image.mode != 'RGB':
            image = image.convert('RGB')
        
        # Convert PIL Image to numpy array
        image_array = np.array(image)
        
        # STEP 2: Run face recognition
        if FACE_RECOGNITION_AVAILABLE:
            # Real face recognition with bounding boxes
            result = extract_face_embedding_real(image_array, image, draw_boxes)
        else:
            # Mock face recognition
            result = extract_face_embedding_mock(image_array, image, draw_boxes)
        
        return result
            
    except Exception as e:
        print(f"❌ Error extracting face embedding: {e}")
        import traceback
        traceback.print_exc()
        
        return {
            'success': False,
            'face_embedding': [],
            'embedding_size': 0,
            'num_faces': 0,
            'face_locations': [],
            'image_with_boxes': None,
            'message': f'Error: {str(e)}',
            'is_mock': False
        }

def extract_face_embedding_real(image_array, original_image, draw_boxes=True):
    """
    Extract face embedding using real face_recognition library with bounding boxes
    
    Args:
        image_array: numpy array of image (RGB)
        original_image: PIL Image object
        draw_boxes: If True, draw green bounding boxes around detected faces
    
    Returns:
        dict: Face embedding result with bounding boxes
    """
    try:
        from PIL import ImageDraw
        import time as py_time
        
        start_processing = py_time.time()
        
        # OPTIMIZATION: Resize image for FASTER face detection
        # 640px is the "Goldilocks" size for CPU detection accuracy vs speed
        height, width = image_array.shape[:2]
        scaling_factor = 1.0
        
        if width > 640:
            scaling_factor = 640.0 / width
            new_width = 640
            new_height = int(height * scaling_factor)
            detection_image = original_image.resize((new_width, new_height), Image.LANCZOS)
            detection_array = np.array(detection_image)
            print(f"🚀 Detection Optimized: {width}x{height} -> 640x{new_height}")
        else:
            detection_array = image_array
            
        # Detect face locations (top, right, bottom, left)
        detect_start = py_time.time()
        face_locations = face_recognition.face_locations(detection_array, model="hog")
        detect_end = py_time.time()
        print(f"⏱️ Face detection took: {detect_end - detect_start:.2f}s (found {len(face_locations)} faces)")
        
        # Resize coordinates back to original image size
        if scaling_factor != 1.0:
            face_locations = [
                (int(t/scaling_factor), int(r/scaling_factor), int(b/scaling_factor), int(l/scaling_factor))
                for (t, r, b, l) in face_locations
            ]
        
        if len(face_locations) == 0:
            return {
                'success': False,
                'face_embedding': [],
                'embedding_size': 0,
                'num_faces': 0,
                'face_locations': [],
                'image_with_boxes': None,
                'message': 'No face detected in image',
                'is_mock': False
            }
        
        # Get face encodings (embeddings)
        # OPTIMIZATION 2: Downsample for encoding if image is still large
        # 640px is plenty for high accuracy encoding
        encode_array = image_array
        if width > 640:
            scale_fac_enc = 640.0 / width
            enc_w = 640
            enc_h = int(height * scale_fac_enc)
            encode_image = original_image.resize((enc_w, enc_h), Image.LANCZOS)
            encode_array = np.array(encode_image)
            
            # Recalculate face locations for the encoding array
            encoded_face_locations = []
            for (top, right, bottom, left) in face_locations:
                encoded_face_locations.append((
                    int(top * scale_fac_enc),
                    int(right * scale_fac_enc),
                    int(bottom * scale_fac_enc),
                    int(left * scale_fac_enc)
                ))
            
            encode_start = py_time.time()
            face_encodings = face_recognition.face_encodings(encode_array, encoded_face_locations, num_jitters=1)
            encode_end = py_time.time()
            print(f"⏱️ Face encoding took: {encode_end - encode_start:.2f}s (Optimized @ 640px)")
        else:
            encode_start = py_time.time()
            face_encodings = face_recognition.face_encodings(image_array, face_locations, num_jitters=1)
            encode_end = py_time.time()
            print(f"⏱️ Face encoding took: {encode_end - encode_start:.2f}s")
        
        if len(face_encodings) == 0:
            return {
                'success': False,
                'face_embedding': [],
                'embedding_size': 0,
                'num_faces': len(face_locations),
                'face_locations': face_locations,
                'image_with_boxes': None,
                'message': 'Face detected but encoding failed',
                'is_mock': False
            }
        
        # Use first face if multiple detected
        face_embedding = face_encodings[0].tolist()
        
        total_process_time = py_time.time() - start_processing
        print(f"✅ Total process time: {total_process_time:.2f}s")
        
        # Draw bounding boxes if requested
        image_with_boxes_base64 = None
        if draw_boxes:
            # Create a copy of the image to draw on
            image_copy = original_image.copy()
            draw = ImageDraw.Draw(image_copy)
            
            # Draw green rectangle around each face
            for (top, right, bottom, left) in face_locations:
                # Draw rectangle with green color (0, 255, 0) and width 3
                draw.rectangle(
                    [(left, top), (right, bottom)],
                    outline=(0, 255, 0),
                    width=3
                )
                
                # Add "Face Detected" label
                draw.text(
                    (left + 6, top - 20),
                    "Face Detected",
                    fill=(0, 255, 0)
                )
            
            # Convert image with boxes to base64
            buffered = io.BytesIO()
            image_copy.save(buffered, format="JPEG", quality=95)
            image_with_boxes_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
            image_with_boxes_base64 = f"data:image/jpeg;base64,{image_with_boxes_base64}"
        
        # Convert face locations to serializable format
        face_locations_list = [
            {
                'top': top,
                'right': right,
                'bottom': bottom,
                'left': left,
                'width': right - left,
                'height': bottom - top
            }
            for (top, right, bottom, left) in face_locations
        ]
        
        return {
            'success': True,
            'face_embedding': face_embedding,
            'embedding_size': len(face_embedding),
            'num_faces': len(face_locations),
            'face_locations': face_locations_list,
            'image_with_boxes': image_with_boxes_base64,
            'message': f'Successfully extracted face embedding ({len(face_locations)} face(s) detected)',
            'is_mock': False
        }
        
    except Exception as e:
        print(f"❌ Error in real face recognition: {e}")
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'face_embedding': [],
            'embedding_size': 0,
            'num_faces': 0,
            'face_locations': [],
            'image_with_boxes': None,
            'message': f'Face recognition error: {str(e)}',
            'is_mock': False
        }

def extract_face_embedding_mock(image_array, original_image, draw_boxes=True):
    """
    Generate mock face embedding for testing (when face_recognition not installed)
    
    Args:
        image_array: numpy array of image (RGB)
        original_image: PIL Image object
        draw_boxes: If True, draw mock green bounding box
    
    Returns:
        dict: Mock face embedding result with mock bounding box
    """
    try:
        from PIL import ImageDraw
        import hashlib
        
        # Create hash from image data
        image_hash = hashlib.md5(image_array.tobytes()).hexdigest()
        
        # Generate 128-dimensional embedding (standard face_recognition size)
        mock_embedding = []
        for i in range(0, 128):
            # Use hash to generate pseudo-random but deterministic values
            hash_segment = image_hash[(i % len(image_hash))]
            value = (int(hash_segment, 16) / 15.0) * 2 - 1  # Scale to [-1, 1]
            mock_embedding.append(float(value))
        
        # Create mock face location (center of image)
        height, width = image_array.shape[:2]
        face_width = int(width * 0.4)
        face_height = int(height * 0.5)
        left = (width - face_width) // 2
        top = (height - face_height) // 2
        right = left + face_width
        bottom = top + face_height
        
        mock_face_location = {
            'top': top,
            'right': right,
            'bottom': bottom,
            'left': left,
            'width': face_width,
            'height': face_height
        }
        
        # Draw mock bounding box if requested
        image_with_boxes_base64 = None
        if draw_boxes:
            image_copy = original_image.copy()
            draw = ImageDraw.Draw(image_copy)
            
            # Draw green rectangle (mock detection)
            draw.rectangle(
                [(left, top), (right, bottom)],
                outline=(0, 255, 0),
                width=3
            )
            
            # Add "MOCK Detection" label
            draw.text(
                (left + 6, top - 20),
                "MOCK Detection",
                fill=(0, 255, 0)
            )
            
            # Convert to base64
            buffered = io.BytesIO()
            image_copy.save(buffered, format="JPEG", quality=95)
            image_with_boxes_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
            image_with_boxes_base64 = f"data:image/jpeg;base64,{image_with_boxes_base64}"
        
        print("⚠️ Using MOCK face embedding (face_recognition not installed)")
        
        return {
            'success': True,
            'face_embedding': mock_embedding,
            'embedding_size': len(mock_embedding),
            'num_faces': 1,
            'face_locations': [mock_face_location],
            'image_with_boxes': image_with_boxes_base64,
            'message': 'MOCK embedding generated (install face-recognition for real detection)',
            'is_mock': True
        }
        
    except Exception as e:
        print(f"❌ Error generating mock embedding: {e}")
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'face_embedding': [],
            'embedding_size': 0,
            'num_faces': 0,
            'face_locations': [],
            'image_with_boxes': None,
            'message': f'Mock generation error: {str(e)}',
            'is_mock': True
        }

def test_face_recognition():
    """Test if face recognition is working"""
    print("\n" + "="*60)
    print("🧪 Testing Face Recognition")
    print("="*60)
    
    if FACE_RECOGNITION_AVAILABLE:
        print("✅ face_recognition library: INSTALLED")
        print("   Status: Real face detection active")
    else:
        print("⚠️ face_recognition library: NOT INSTALLED")
        print("   Status: Using MOCK mode")
        print("\n   To install:")
        print("   pip install face-recognition")
    
    print("="*60 + "\n")

if __name__ == '__main__':
    # Run test when executed directly
    test_face_recognition()
