from infographic_agent.config import settings
from infographic_agent.rendering.browser_pool import get_browser


def render_to_png(html: str) -> bytes:
    with get_browser() as browser:
        page = browser.new_page(
            viewport={"width": settings.canvas_width, "height": settings.canvas_height},
            device_scale_factor=settings.device_scale_factor,
        )
        try:
            page.set_content(html, wait_until="load")
            return page.screenshot(type="png")
        finally:
            page.close()
