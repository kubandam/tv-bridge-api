"""
Server-side CLIP ad detector (lazy singleton).

Loads CLIP + classifier.pkl on first call. Subsequent calls reuse the loaded model.
First inference ~30-60s (model download). Subsequent ~1-3s on CPU.
"""

from __future__ import annotations

import io
import pickle
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

_lock = threading.Lock()
_detector: "_Detector | None" = None

MODEL_PATH = Path(__file__).parent.parent.parent / "models" / "classifier.pkl"


def get_detector() -> "_Detector":
    global _detector
    if _detector is None:
        with _lock:
            if _detector is None:
                _detector = _Detector()
    return _detector


def detect_bytes(image_bytes: bytes) -> dict[str, Any]:
    """Run ad detection on raw JPEG/PNG bytes."""
    return get_detector().detect(image_bytes)


def model_info() -> dict[str, Any]:
    if not MODEL_PATH.exists():
        return {"loaded": False, "error": "models/classifier.pkl not found"}
    if _detector is None:
        return {"loaded": False, "model_path": str(MODEL_PATH)}
    return _detector.info()


class _Detector:
    def __init__(self):
        import torch
        import clip as openai_clip

        if not MODEL_PATH.exists():
            raise FileNotFoundError(f"Model not found: {MODEL_PATH}")

        with open(MODEL_PATH, "rb") as fh:
            payload = pickle.load(fh)

        self.classifier = payload["classifier"]
        self.clip_model_name: str = payload.get("clip_model", "ViT-B/32")
        self.threshold: float = payload.get("threshold", 0.55)
        self.use_image_stats: bool = payload.get("use_image_stats", True)
        self.model_name: str = payload.get("best_model_name", "classifier")
        self.cv_precision: float = payload.get("cv_precision", 0.0)
        self.cv_recall: float = payload.get("cv_recall", 0.0)
        self.cv_f1: float = payload.get("cv_f1", 0.0)
        self.train_samples: dict = payload.get("train_samples", {})

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.clip_model, self.preprocess = openai_clip.load(self.clip_model_name, device=self.device)
        self.clip_model.eval()

    @staticmethod
    def _image_stats(img: Image.Image) -> np.ndarray:
        img_s = img.resize((112, 112))
        arr = np.array(img_s).astype(np.float32) / 255.0
        r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
        max_c = arr.max(axis=2)
        sat = np.where(max_c > 1e-6, (max_c - arr.min(axis=2)) / max_c, 0.0)
        bright = max_c
        gray = 0.299 * r + 0.587 * g + 0.114 * b
        return np.array([
            r.mean(), g.mean(), b.mean(),
            r.std(), g.std(), b.std(),
            sat.mean(), sat.std(),
            bright.mean(), bright.std(),
            gray.std(),
            float(np.percentile(sat, 75)),
            float(np.percentile(bright, 25)),
            float(np.percentile(bright, 75)),
        ], dtype=np.float32)

    def detect(self, image_bytes: bytes) -> dict[str, Any]:
        import torch

        t0 = time.perf_counter()
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

        with torch.no_grad():
            tensor = self.preprocess(img).unsqueeze(0).to(self.device)
            feat = self.clip_model.encode_image(tensor)
            feat = feat / feat.norm(dim=-1, keepdim=True)
            clip_feat = feat.cpu().numpy()[0]

        if self.use_image_stats:
            stats = self._image_stats(img)
            features = np.concatenate([clip_feat, stats]).reshape(1, -1)
        else:
            features = clip_feat.reshape(1, -1)

        p_ad = float(self.classifier.predict_proba(features)[0][1])
        is_ad = p_ad >= self.threshold
        detect_ms = int((time.perf_counter() - t0) * 1000)

        return {
            "is_ad": is_ad,
            "confidence": round(p_ad, 4),
            "threshold": self.threshold,
            "model_name": self.model_name,
            "clip_model": self.clip_model_name,
            "cv_precision": self.cv_precision,
            "cv_recall": self.cv_recall,
            "detect_ms": detect_ms,
        }

    def info(self) -> dict[str, Any]:
        return {
            "loaded": True,
            "model_name": self.model_name,
            "clip_model": self.clip_model_name,
            "threshold": self.threshold,
            "cv_precision": self.cv_precision,
            "cv_recall": self.cv_recall,
            "cv_f1": self.cv_f1,
            "train_samples": self.train_samples,
            "device": self.device,
        }
