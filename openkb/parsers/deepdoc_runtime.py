"""Load packaged DeepDoc OCR assets for enhanced PDF parsing.

OpenKB keeps its parser small by using compatible DeepDoc detection and
recognition ONNX models through RapidOCR, while the PDF adapter retains page,
layout, and table evidence itself.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_OCR_MODEL_FILES = ("det.onnx", "rec.onnx", "ocr.res")


def deepdoc_ocr_engine() -> Any | None:
    """Return a DeepDoc-backed RapidOCR engine when the portable bundle provides it.

    Development installs use RapidOCR's own bundled models.  A frozen portable
    Engine must find its explicit DeepDoc model bundle so first use cannot fall
    back to a download or silently lose the release parser capability.
    """
    runtime_directory = _runtime_directory()
    if runtime_directory is None:
        return None
    model_paths = tuple(runtime_directory / filename for filename in _OCR_MODEL_FILES)
    if not all(path.is_file() for path in model_paths):
        if _is_frozen():
            raise RuntimeError("The packaged DeepDoc OCR model bundle is incomplete.")
        return None

    try:
        from rapidocr_onnxruntime.cal_rec_boxes import CalRecBoxes
        from rapidocr_onnxruntime.ch_ppocr_cls import TextClassifier
        from rapidocr_onnxruntime.ch_ppocr_det import TextDetector
        from rapidocr_onnxruntime.ch_ppocr_rec import TextRecognizer
        from rapidocr_onnxruntime.main import DEFAULT_CFG_PATH, RapidOCR, root_dir
        from rapidocr_onnxruntime.utils import LoadImage, read_yaml
    except ImportError as error:
        raise RuntimeError("The packaged ONNX OCR runtime is unavailable.") from error

    configuration = read_yaml(DEFAULT_CFG_PATH)
    detector_path, recognizer_path, character_path = model_paths
    configuration["Det"]["model_path"] = str(detector_path)
    configuration["Rec"]["model_path"] = str(recognizer_path)
    configuration["Rec"]["rec_keys_path"] = str(character_path)
    configuration["Cls"]["model_path"] = str(root_dir / configuration["Cls"]["model_path"])

    global_configuration = configuration["Global"]
    engine = RapidOCR.__new__(RapidOCR)
    engine.print_verbose = global_configuration["print_verbose"]
    engine.text_score = global_configuration["text_score"]
    engine.min_height = global_configuration["min_height"]
    engine.width_height_ratio = global_configuration["width_height_ratio"]
    engine.use_det = global_configuration["use_det"]
    engine.text_det = TextDetector(configuration["Det"])
    engine.use_cls = global_configuration["use_cls"]
    engine.text_cls = TextClassifier(configuration["Cls"])
    engine.use_rec = global_configuration["use_rec"]
    engine.text_rec = TextRecognizer(configuration["Rec"])
    engine.load_img = LoadImage()
    engine.max_side_len = global_configuration["max_side_len"]
    engine.min_side_len = global_configuration["min_side_len"]
    engine.cal_rec_boxes = CalRecBoxes()
    return engine


def _runtime_directory() -> Path | None:
    configured = os.environ.get("OPENKB_DEEPDOC_RUNTIME")
    if configured:
        return Path(configured).expanduser().resolve()
    bundle_root = getattr(sys, "_MEIPASS", None)
    return Path(bundle_root) / "deepdoc" if bundle_root is not None else None


def _is_frozen() -> bool:
    return getattr(sys, "_MEIPASS", None) is not None
