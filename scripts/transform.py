#!/usr/bin/env python3
"""
Depth Estimation Privacy Skill — Monocular depth maps via Depth Anything v2.

Backend selection:
  macOS  → CoreML (.mlpackage via coremltools) — runs on Neural Engine
  Other  → PyTorch (depth_anything_v2 pip package + HF weights) — runs on CUDA/MPS/CPU

Implements the TransformSkillBase interface to provide real-time depth map
overlays on camera feeds. When used as a privacy skill, the depth-only mode
anonymizes the scene while preserving spatial layout and activity recognition.

Usage:
  python transform.py --model depth-anything-v2-small --device auto
  python transform.py --config config.json
"""

import sys
import os
import platform
import argparse
from pathlib import Path

# Import the base class from the same directory
_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir))

from transform_base import TransformSkillBase, _log  # noqa: E402


COLORMAP_MAP = {
    "inferno": 1,   # cv2.COLORMAP_INFERNO
    "viridis": 16,  # cv2.COLORMAP_VIRIDIS
    "plasma": 13,   # cv2.COLORMAP_PLASMA
    "magma": 12,    # cv2.COLORMAP_MAGMA
    "jet": 2,       # cv2.COLORMAP_JET
    "turbo": 18,    # cv2.COLORMAP_TURBO
    "hot": 11,      # cv2.COLORMAP_HOT
    "cool": 8,      # cv2.COLORMAP_COOL
}

# CoreML model registry — mirrors apple/coreml-depth-anything-v2-small HF repo
COREML_VARIANTS = {
    "DepthAnythingV2SmallF16": {
        "precision": "float16",
        "size_mb": 49.8,
        "description": "Float16 — optimized for Neural Engine",
    },
    "DepthAnythingV2SmallF16INT8": {
        "precision": "float16_int8",
        "size_mb": 25.0,
        "description": "Float16 + INT8 quantization — smallest",
    },
    "DepthAnythingV2SmallF32": {
        "precision": "float32",
        "size_mb": 99.2,
        "description": "Float32 — highest precision",
    },
}

# Default CoreML variant (best balance of speed + quality on Neural Engine)
DEFAULT_COREML_VARIANT = "DepthAnythingV2SmallF16"

# HuggingFace repo for CoreML models
COREML_HF_REPO = "apple/coreml-depth-anything-v2-small"

# CoreML input size — MUST match model exactly (multiples of 14 for ViT)
COREML_INPUT_SIZE = (518, 392)  # width, height

# Where Aegis DepthVisionStudio stores downloaded models
MODELS_DIR = Path.home() / ".aegis-ai" / "models" / "feature-extraction"

# PyTorch model configs (fallback on non-macOS)
PYTORCH_CONFIGS = {
    "depth-anything-v2-small": {
        "encoder": "vits", "features": 64,
        "out_channels": [48, 96, 192, 384],
        "repo": "depth-anything/Depth-Anything-V2-Small",
        "filename": "depth_anything_v2_vits.pth",
    },
    "depth-anything-v2-base": {
        "encoder": "vitb", "features": 128,
        "out_channels": [96, 192, 384, 768],
        "repo": "depth-anything/Depth-Anything-V2-Base",
        "filename": "depth_anything_v2_vitb.pth",
    },
    "depth-anything-v2-large": {
        "encoder": "vitl", "features": 256,
        "out_channels": [256, 512, 1024, 1024],
        "repo": "depth-anything/Depth-Anything-V2-Large",
        "filename": "depth_anything_v2_vitl.pth",
    },
}


class DepthEstimationSkill(TransformSkillBase):
    """
    Depth estimation using Depth Anything v2.

    Produces colorized depth maps that can be blended with the original frame
    (overlay mode), shown side-by-side, or displayed as depth-only anonymized view.
    """

    def __init__(self):
        super().__init__()
        self._tag = "DepthEstimation"
        self.model = None
        self.backend = None  # "coreml" or "pytorch"
        self.colormap_id = 16  # cv2.COLORMAP_VIRIDIS
        self.opacity = 0.5
        self.blend_mode = "depth_only"  # Default for privacy: depth_only anonymizes
        self._coreml_input_size = COREML_INPUT_SIZE

    def parse_extra_args(self, parser: argparse.ArgumentParser):
        parser.add_argument("--model", type=str, default="depth-anything-v2-small",
                            choices=["depth-anything-v2-small", "depth-anything-v2-base",
                                     "depth-anything-v2-large"])
        parser.add_argument("--variant", type=str, default=DEFAULT_COREML_VARIANT,
                            help="CoreML variant ID (macOS only)")
        parser.add_argument("--colormap", type=str, default="viridis",
                            choices=list(COLORMAP_MAP.keys()))
        parser.add_argument("--blend-mode", type=str, default="depth_only",
                            choices=["overlay", "side_by_side", "depth_only"])
        parser.add_argument("--opacity", type=float, default=0.5)

    def load_model(self, config: dict) -> dict:
        model_name = config.get("model", "depth-anything-v2-small")
        self.colormap_id = COLORMAP_MAP.get(config.get("colormap", "viridis"), 16)
        self.opacity = config.get("opacity", 0.5)
        self.blend_mode = config.get("blend_mode", "depth_only")

        # Try CoreML first on macOS
        if platform.system() == "Darwin":
            try:
                info = self._load_coreml(config)
                return info
            except Exception as e:
                _log(f"CoreML load failed ({e}), falling back to PyTorch", self._tag)

        # Fallback: PyTorch
        return self._load_pytorch(model_name, config)

    # ── CoreML backend (macOS) ────────────────────────────────────────

    def _load_coreml(self, config: dict) -> dict:
        """Load CoreML .mlpackage model — runs on Apple Neural Engine."""
        import coremltools as ct

        variant_id = config.get("variant", DEFAULT_COREML_VARIANT)
        model_path = MODELS_DIR / f"{variant_id}.mlpackage"

        # Auto-download from HuggingFace if not present
        if not model_path.exists():
            _log(f"CoreML model not found at {model_path}, downloading from HF...", self._tag)
            self._download_coreml_model(variant_id)

        if not model_path.exists():
            raise FileNotFoundError(f"CoreML model not found: {model_path}")

        _log(f"Loading CoreML model: {variant_id} (Neural Engine)", self._tag)
        self.model = ct.models.MLModel(str(model_path), compute_units=ct.ComputeUnit.ALL)
        self.backend = "coreml"

        _log(f"CoreML model loaded: {variant_id}", self._tag)
        return {
            "model": f"coreml-{variant_id}",
            "device": "neural_engine",
            "blend_mode": self.blend_mode,
            "colormap": config.get("colormap", "viridis"),
            "backend": "coreml",
        }

    def _download_coreml_model(self, variant_id: str):
        """Download CoreML .mlpackage from HuggingFace using huggingface_hub."""
        try:
            from huggingface_hub import snapshot_download

            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            mlpackage_name = f"{variant_id}.mlpackage"

            _log(f"Downloading {mlpackage_name} from {COREML_HF_REPO}...", self._tag)

            # Download only the specific variant's .mlpackage directory
            snapshot_download(
                COREML_HF_REPO,
                local_dir=str(MODELS_DIR),
                allow_patterns=[f"{mlpackage_name}/**"],
            )

            model_path = MODELS_DIR / mlpackage_name
            if model_path.exists():
                _log(f"Downloaded CoreML model: {model_path}", self._tag)
            else:
                _log(f"Download completed but model not found at {model_path}", self._tag)
        except Exception as e:
            _log(f"CoreML model download failed: {e}", self._tag)
            raise

    # ── PyTorch backend (fallback) ────────────────────────────────────

    def _load_pytorch(self, model_name: str, config: dict) -> dict:
        """Load PyTorch model — fallback for non-macOS or when CoreML is unavailable."""
        import torch
        from depth_anything_v2.dpt import DepthAnythingV2
        from huggingface_hub import hf_hub_download

        _log(f"Loading {model_name} on {self.device} (PyTorch)", self._tag)

        cfg = PYTORCH_CONFIGS.get(model_name)
        if not cfg:
            raise ValueError(f"Unknown model: {model_name}. Choose from: {list(PYTORCH_CONFIGS.keys())}")

        # Download weights from HuggingFace Hub (cached after first download)
        _log(f"Downloading weights from HF: {cfg['repo']}", self._tag)
        weights_path = hf_hub_download(cfg["repo"], cfg["filename"])

        # Build model from pip package
        self.model = DepthAnythingV2(
            encoder=cfg["encoder"],
            features=cfg["features"],
            out_channels=cfg["out_channels"],
        )
        self.model.load_state_dict(torch.load(weights_path, map_location=self.device, weights_only=True))
        self.model.to(self.device)
        self.model.eval()
        self.backend = "pytorch"

        _log(f"PyTorch model loaded: {model_name} on {self.device}", self._tag)
        return {
            "model": model_name,
            "device": self.device,
            "blend_mode": self.blend_mode,
            "colormap": config.get("colormap", "viridis"),
            "backend": "pytorch",
        }

    # ── Frame transform ───────────────────────────────────────────────

    def transform_frame(self, image, metadata: dict):
        import cv2
        import numpy as np

        if self.backend == "coreml":
            depth_colored = self._infer_coreml(image)
        else:
            depth_colored = self._infer_pytorch(image)

        if self.blend_mode == "overlay":
            output = cv2.addWeighted(image, 1 - self.opacity, depth_colored, self.opacity, 0)
        elif self.blend_mode == "side_by_side":
            output = np.hstack([image, depth_colored])
        else:  # depth_only — full anonymization
            output = depth_colored

        return output

    def _infer_coreml(self, image):
        """Run CoreML inference and return colorized depth map (BGR, original size)."""
        import cv2
        import numpy as np
        from PIL import Image

        original_h, original_w = image.shape[:2]
        input_w, input_h = self._coreml_input_size

        # BGR → RGB → resize to model input → PIL
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(rgb, (input_w, input_h), interpolation=cv2.INTER_LINEAR)
        pil_image = Image.fromarray(resized, mode="RGB")

        # Inference
        prediction = self.model.predict({"image": pil_image})

        # Extract depth map (first output key)
        output_key = list(prediction.keys())[0]
        depth_map = prediction[output_key]

        # Convert PIL Image to numpy if needed
        if isinstance(depth_map, Image.Image):
            depth_map = np.array(depth_map)

        depth_map = np.array(depth_map)
        if depth_map.ndim > 2:
            depth_map = np.squeeze(depth_map)

        # Normalize → uint8 → colormap → resize back
        depth_norm = (depth_map - depth_map.min()) / (depth_map.max() - depth_map.min() + 1e-8)
        depth_uint8 = (depth_norm * 255).astype(np.uint8)
        depth_colored = cv2.applyColorMap(depth_uint8, self.colormap_id)
        depth_colored = cv2.resize(depth_colored, (original_w, original_h))

        return depth_colored

    def _infer_pytorch(self, image):
        """Run PyTorch inference and return colorized depth map (BGR, original size)."""
        import torch
        import cv2
        import numpy as np

        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

        with torch.no_grad():
            depth = self.model.infer_image(rgb)

        d_min, d_max = depth.min(), depth.max()
        depth_norm = ((depth - d_min) / (d_max - d_min + 1e-8) * 255).astype(np.uint8)
        depth_colored = cv2.applyColorMap(depth_norm, self.colormap_id)

        return depth_colored

    # ── Config updates ────────────────────────────────────────────────

    def on_config_update(self, config: dict):
        """Handle live config updates from Aegis."""
        if "colormap" in config:
            self.colormap_id = COLORMAP_MAP.get(config["colormap"], self.colormap_id)
            _log(f"Colormap updated: {config['colormap']}", self._tag)
        if "opacity" in config:
            self.opacity = float(config["opacity"])
            _log(f"Opacity updated: {self.opacity}", self._tag)
        if "blend_mode" in config:
            self.blend_mode = config["blend_mode"]
            _log(f"Blend mode updated: {self.blend_mode}", self._tag)

    def get_output_mode(self) -> str:
        """Use base64 for privacy transforms — avoids temp file cleanup issues."""
        return "base64"


if __name__ == "__main__":
    DepthEstimationSkill().run()
