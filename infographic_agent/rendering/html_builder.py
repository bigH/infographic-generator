from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel

from infographic_agent.config import settings
from infographic_agent.templates.registry import TemplateSpec

_env = Environment(loader=FileSystemLoader(str(settings.templates_dir)))


def build_html(template_spec: TemplateSpec, slots: BaseModel, image_srcs: dict[str, str]) -> str:
    template = _env.get_template(f"{template_spec.id}/template.html.j2")
    css = template_spec.css_path.read_text()
    return template.render(
        slots=slots,
        canvas_width=settings.canvas_width,
        canvas_height=settings.canvas_height,
        template_css=css,
        **image_srcs,
    )
