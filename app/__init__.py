from flask import Flask

from config import Config
from .extensions import csrf, db, login_manager
from .models import User


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    login_manager.login_view = "main.login"
    login_manager.login_message = "Entre para acessar seu financeiro."
    login_manager.login_message_category = "warning"

    from .routes import main
    app.register_blueprint(main)

    with app.app_context():
        db.create_all()

    return app


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

