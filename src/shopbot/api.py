from .config import settings
from .enhanced import create_app

app = create_app(settings)
