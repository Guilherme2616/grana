from getpass import getpass

from app import create_app
from app.extensions import db
from app.models import Category, User


app = create_app()
with app.app_context():
    username = input("Nome de usuário: ").strip()
    password = getpass("Senha: ")
    if len(password) < 8:
        raise SystemExit("Use uma senha com pelo menos 8 caracteres.")
    user = User.query.filter_by(username=username).first() or User(username=username)
    user.set_password(password)
    db.session.add(user)
    if Category.query.count() == 0:
        defaults = [("Alimentação", "expense", "#D98A61", "F"), ("Moradia", "expense", "#728C7A", "⌂"), ("Transporte", "expense", "#7A6C91", "T"), ("Lazer", "expense", "#D8B56A", "✦"), ("Salário", "income", "#5F8A68", "$")]
        for name, kind, color, icon in defaults: db.session.add(Category(name=name, kind=kind, color=color, icon=icon))
    db.session.commit()
    print("Usuário criado e categorias iniciais adicionadas.")
