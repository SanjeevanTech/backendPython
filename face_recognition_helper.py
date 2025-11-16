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

def setup_image_directories():
    """
    Create necessary directories for image storage
    Returns paths dict
    """
    base_dir = Path(__file__).parent
    
    paths = {
        'temp': base_dir / 'images' / 'temp',
        'permanent': base_dir / 'images' / 'permanent',
        'failed': base_dir / 'images' / 'failed'
    }
    
    # Create directories if they don't exist
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    
    return paths

def save_image_to_disk(image_data_base64, user_id=None, detection_type='entry', storage_type='temp'):
    """
    Save image to disk BEFORE processing (best practice)
    
    Args:
        image_data_base64: Base64 encoded image
        user_id: User/passenger identifier (optional)
        detection_type: 'entry' or 'exit'
        storage_type: 'temp', 'permanent', or 'failed'
    
    Returns:
        dict: {
            'success': bool,
            'file_path': str,
            'filename': str,
            'message': str
        }
    """
    try:
        # Setup directories
        paths = setup_image_directories()
        
        # Remove data URI prefix if present
        if ',' in image_data_base64:
            image_data_base64 = image_data_base64.split(',')[1]
        
        # Decode image
        image_bytes = base64.b64decode(image_data_base64)
        
        # Generate filename with timestamp
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        user_prefix = f"{user_id}_" if user_id else ""
        filename = f"{user_prefix}{detection_type}_{timestamp}.jpg"
        
        # Determine storage path
        storage_path = paths.get(storage_type, paths['temp'])
        file_path = storage_path / filename
        
        # Save image
        with open(file_path, 'wb') as f:
            f.write(image_bytes)
        
        print(f"✅ Image saved: {file_path}")
        
        return {
            'success': True,
            'file_path': str(file_path),
            'filename': filename,
            'storage_type': storage_type,
            'message': f'Image saved to {storage_type} storage'
        }
        
    except Exception as e:
        print(f"❌ Error saving image: {e}")
        return {
            'success': False,
            'file_path': None,
            'filename': None,
            'message': f'Failed to save image: {str(e)}'
        }

def move_image(source_path, destination_type='permanent'):
    """
    Move image from temp to permanent storage after successful processing
    
    Args:
        source_path: Current file path
        destination_type: 'permanent' or 'failed'
    
    Returns:
        dict: {
            'success': bool,
            'new_path': str,
            'message': str
        }
    """
    try:
        source = Path(source_path)
        if not source.exists():
            return {
                'success': False,
                'new_path': None,
                'message': 'Source file not found'
            }
        
        # Setup directories
        paths = setup_image_directories()
        destination_dir = paths.get(destination_type, paths['permanent'])
        
        # New path with same filename
        new_path = destination_dir / source.name
        
        # Move file
        source.rename(new_path)
        
        print(f"✅ Image moved: {source} → {new_path}")
        
        return {
            'success': True,
            'new_path': str(new_path),
            'message': f'Image moved to {destination_type} storage'
        }
        
    except Exception as e:
        print(f"❌ Error moving image: {e}")
        return {
            'success': False,
            'new_path': None,
            'message': f'Failed to move image: {str(e)}'
        }

def cleanup_old_temp_images(hours=24):
    """
    Delete temporary images older than specified hours
    
    Args:
        hours: Age threshold in hours (default 24)
    
    Returns:
        int: Number of files deleted
    """
    try:
        paths = setup_image_directories()
        temp_dir = paths['temp']
        
        deleted_count = 0
        cutoff_time = datetime.now().timestamp() - (hours * 3600)
        
        for file_path in temp_dir.glob('*.jpg'):
            if file_path.stat().st_mtime < cutoff_time:
                file_path.unlink()
                deleted_count += 1
                print(f"🗑️ Deleted old temp image: {file_path.name}")
        
        if deleted_count > 0:
            print(f"✅ Cleaned up {deleted_count} old temp images")
        
        return deleted_count
        
    except Exception as e:
        print(f"❌ Error cleaning up temp images: {e}")
        return 0

def extract_face_embedding_from_base64(image_data_base64, draw_boxes=True, user_id=None, detection_type='entry', save_image=True):
    """
    Extract face embedding from base64 encoded image with optional bounding boxes
    BEST PRACTICE: Saves image BEFORE processing for debugging and audit trail
    
    Args:
        image_data_base64: Base64 encoded image string (with or without data URI prefix)
        draw_boxes: If True, draw green bounding boxes around detected faces
        user_id: User/passenger identifier for filename (optional)
        detection_type: 'entry' or 'exit' for filename
        save_image: If True, save image to disk before processing
    
    Returns:
        dict: {
            'success': bool,
            'face_embedding': list,
            'embedding_size': int,
            'num_faces': int,
            'face_locations': list,
            'image_with_boxes': str (base64),
            'message': str,
            'is_mock': bool,
            'saved_image': dict (file info if saved)
        }
    """
    saved_image_info = None
    
    try:
        # STEP 1: Save image FIRST (before processing) - BEST PRACTICE
        if save_image:
            saved_image_info = save_image_to_disk(
                image_data_base64, 
                user_id=user_id, 
                detection_type=detection_type,
                storage_type='temp'
            )
            if not saved_image_info['success']:
                print(f"⚠️ Warning: Failed to save image, continuing with processing")
        
        # STEP 2: Process the image
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
        
        # STEP 3: Run face recognition
        if FACE_RECOGNITION_AVAILABLE:
            # Real face recognition with bounding boxes
            result = extract_face_embedding_real(image_array, image, draw_boxes)
        else:
            # Mock face recognition
            result = extract_face_embedding_mock(image_array, image, draw_boxes)
        
        # STEP 4: Move image based on success/failure
        if saved_image_info and saved_image_info['success']:
            if result['success']:
                # Success: Move to permanent storage
                move_result = move_image(saved_image_info['file_path'], 'permanent')
                saved_image_info['final_path'] = move_result.get('new_path')
                saved_image_info['final_storage'] = 'permanent'
            else:
                # Failed: Move to failed storage for debugging
                move_result = move_image(saved_image_info['file_path'], 'failed')
                saved_image_info['final_path'] = move_result.get('new_path')
                saved_image_info['final_storage'] = 'failed'
        
        # Add saved image info to result
        result['saved_image'] = saved_image_info
        
        return result
            
    except Exception as e:
        print(f"❌ Error extracting face embedding: {e}")
        import traceback
        traceback.print_exc()
        
        # If we saved the image but processing failed, move to failed storage
        if saved_image_info and saved_image_info['success']:
            move_image(saved_image_info['file_path'], 'failed')
        
        return {
            'success': False,
            'face_embedding': [],
            'embedding_size': 0,
            'num_faces': 0,
            'face_locations': [],
            'image_with_boxes': None,
            'message': f'Error: {str(e)}',
            'is_mock': False,
            'saved_image': saved_image_info
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
        
        # Detect face locations (top, right, bottom, left)
        face_locations = face_recognition.face_locations(image_array)
        
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
        face_encodings = face_recognition.face_encodings(image_array, face_locations)
        
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
