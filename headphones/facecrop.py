import os

from headphones import logger

try:
    import cv2
except ImportError:  # pragma: no cover - optional dependency
    cv2 = None

MODEL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'models', 'face_detection_yunet_2023mar.onnx'
)


def detect_face_center_y(image_path):
    """
    Runs face detection on a local image file and returns the vertical
    center of the largest detected face as a percentage (0-100) of the
    image's height, for biasing a CSS object-position crop toward the
    actual face instead of a fixed guess. Returns None if OpenCV/the
    model aren't available, the file can't be read, or no face is found
    (e.g. the "artist" image is really album art with no face in it).
    """
    if cv2 is None or not os.path.exists(MODEL_PATH):
        return None

    try:
        img = cv2.imread(image_path)
        if img is None:
            return None

        height, width = img.shape[:2]
        if not height or not width:
            return None

        detector = cv2.FaceDetectorYN_create(MODEL_PATH, '', (width, height))
        _, faces = detector.detect(img)

        if faces is None or len(faces) == 0:
            return None

        # Multiple faces (band photo, etc.) -- go with the biggest one,
        # it's the most likely to be the actual subject of the crop.
        best = max(faces, key=lambda f: f[2] * f[3])
        face_y, face_h = float(best[1]), float(best[3])

        return round((face_y + face_h / 2) / height * 100, 1)
    except Exception as e:
        logger.debug(f"Face detection failed for {image_path}: {e}")
        return None
