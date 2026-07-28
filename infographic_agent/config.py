from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    canvas_width: int = 1080
    canvas_height: int = 1350
    device_scale_factor: int = 2

    llm_model: str = "claude-opus-5"

    templates_dir: Path = Path(__file__).parent / "templates"
    placeholder_image_path: Path = Path(__file__).parent / "images" / "assets" / "placeholder.svg"


settings = Settings()
