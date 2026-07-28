from infographic_agent.config import settings
from infographic_agent.images.encode import file_to_data_uri


def placeholder_data_uri() -> str:
    return file_to_data_uri(settings.placeholder_image_path)
