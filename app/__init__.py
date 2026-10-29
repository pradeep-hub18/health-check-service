from flask import Flask
from app.database import init_db

def create_app(*, initialize_database=True):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

    if initialize_database:
        init_db()

    from app.routes import register_routes
    register_routes(app)

    return app
