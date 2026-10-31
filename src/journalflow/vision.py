from __future__ import annotations

from pathlib import Path

from .config import Vision


class PaddleVision:
    """Full layout + region-recognition pipeline, with a separate vLLM model service."""

    def __init__(self, config: Vision):
        try:
            from paddleocr import PaddleOCRVL
        except ImportError as error:
            raise RuntimeError("Install the vision extra and PaddlePaddle; see docs/vision.md") from error
        self.config = config
        self.pipeline = PaddleOCRVL(
            pipeline_version="v1.6", device=config.layout_device,
            vl_rec_backend="vllm-server", vl_rec_server_url=config.base_url,
            vl_rec_api_model_name=config.model, vl_rec_max_concurrency=config.concurrency,
            use_doc_orientation_classify=False, use_doc_unwarping=False,
            use_chart_recognition=False, use_ocr_for_image_block=True, format_block_content=True,
            markdown_ignore_labels=["header", "header_image", "footer", "footer_image"])

    def recognize(self, images: list, directories: list[Path], *, max_pixels: int):
        results = self.pipeline.predict(
            images, max_pixels=max_pixels, max_new_tokens=self.config.max_new_tokens,
            temperature=0.0, top_p=1.0,
            markdown_ignore_labels=["header", "header_image", "footer", "footer_image"])
        saved = 0
        for result, directory in zip(results, directories, strict=True):
            directory.mkdir(parents=True, exist_ok=True)
            result["input_path"] = directory.name + ".png"
            result.save_to_json(save_path=str(directory / "result.json"))
            result.save_to_markdown(save_path=str(directory / "page.md"), pretty=False,
                                    show_formula_number=True)
            saved += 1
        if saved != len(directories):
            raise ValueError("Recognition did not yield exactly one result per page")
