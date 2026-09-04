from .app_v2 import create_app
from .config import settings

app = create_app(settings)
