from .config import settings
from .runtime import create_app

app = create_app(settings)
