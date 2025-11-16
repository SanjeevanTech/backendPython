# Face Recognition Image Storage

This directory stores captured images from the face recognition system.

## Directory Structure

```
images/
├── temp/          # Temporary storage (images captured but not yet processed)
├── permanent/     # Successfully processed images (face detected)
└── failed/        # Failed detections (for debugging)
```

## Image Capture Workflow

### Best Practice: Capture BEFORE Processing

1. **Camera captures image** → Save immediately to `temp/` storage
2. **Run face recognition** on saved image
3. **If face detected successfully:**
   - Move image to `permanent/` storage
   - Store face encoding in database
   - Link image path to user record
4. **If face detection fails:**
   - Move image to `failed/` storage for debugging
   - Show error to user with option to retry
   - Can reprocess same image without recapture

## Filename Format

```
[user_id]_[type]_[timestamp].jpg
```

Examples:
- `PASS_000123_entry_20231116_143022_123456.jpg`
- `entry_20231116_143022_123456.jpg` (no user_id yet)
- `exit_20231116_150530_789012.jpg`

## Automatic Cleanup

- Temp images older than 24 hours are automatically deleted
- Permanent images are kept indefinitely
- Failed images are kept for debugging (manual cleanup recommended)

## Storage Considerations

- **Temp folder**: Auto-cleanup after 24 hours
- **Permanent folder**: Keep for audit trail and reprocessing
- **Failed folder**: Review periodically for quality issues

## Privacy & Security

- Images contain personal data (faces)
- Ensure proper access controls on this directory
- Consider encryption for permanent storage
- Implement data retention policies per local regulations

## Debugging Tips

If face recognition fails:
1. Check `failed/` folder for image quality issues
2. Look for: blur, poor lighting, face angle, occlusion
3. Can reprocess images from `failed/` folder without asking user to pose again
4. Adjust camera settings or lighting based on failed images

## API Usage

```python
from face_recognition_helper import extract_face_embedding_from_base64

# Process image with automatic saving
result = extract_face_embedding_from_base64(
    image_data_base64=base64_image,
    user_id="PASS_000123",
    detection_type="entry",
    save_image=True  # Default: saves before processing
)

# Check saved image info
if result['saved_image']:
    print(f"Image saved to: {result['saved_image']['final_path']}")
    print(f"Storage type: {result['saved_image']['final_storage']}")
```

## Maintenance

Run cleanup manually if needed:
```python
from face_recognition_helper import cleanup_old_temp_images

# Delete temp images older than 24 hours
deleted = cleanup_old_temp_images(hours=24)
print(f"Deleted {deleted} old temp images")
```
