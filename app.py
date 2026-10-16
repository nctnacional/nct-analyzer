import os
from datetime import datetime
from flask import Flask, render_template, redirect, url_for, request, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash

# 1. Inicialização do Aplicativo
app = Flask(__name__)

# Configurações básicas
app.config['WTF_CSRF_ENABLED'] = False  # Desativa o CSRF globalmente para evitar erros de token ausente
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "sua-chave-secreta-padrao-aqui")

# Configuração da Base de Dados (compatível com SQLite local e PostgreSQL no Render)
database_url = os.environ.get("DATABASE_URL", "").strip()
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

if not database_url:
    database_url = "sqlite:///nct_online.db"

if database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+psycopg2://", 1)

app.config.update(
    SQLALCHEMY_DATABASE_URI=database_url,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,
)

# 2. Inicialização do SQLAlchemy e LoginManager
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Faça login para continuar."
login_manager.login_message_category = "warning"

csrf = CSRFProtect(app)


# 3. Definição Única do Modelo User
class User(db.Model):
    __tablename__ = 'user'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_active_account = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def get_id(self):
        return str(self.id)

    @property
    def is_authenticated(self):
        return True

    @property
    def is_active(self):
        return self.is_active_account

    @property
    def is_anonymous(self):
        return False


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# 4. Criação automática das tabelas ao iniciar
with app.app_context():
    db.create_all()


# 5. Rotas da Aplicação

@app.route("/")
@login_required
def index():
    return render_template("index.html")  # Substitua pelo template principal do seu painel


@app.route("/login", methods=["GET", "POST"])
def login():
    try:
        if current_user.is_authenticated:
            return redirect(url_for("index"))

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")

            user = User.query.filter_by(email=email).first()

            if user and check_password_hash(user.password_hash, password):
                if user.is_active_account:
                    login_user(user)
                    return redirect(url_for("index"))
                else:
                    flash("Sua conta está desativada.", "danger")
            else:
                flash("E-mail ou senha incorretos.", "danger")

        return render_template("login.html")
        
    except Exception as e:
        app.logger.exception("Erro crítico na rota /login")
        flash(f"Erro interno: {str(e)}", "danger")
        return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    try:
        if current_user.is_authenticated:
            return redirect(url_for("index"))

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")

            if not email or not password:
                flash("Preencha todos os campos.", "danger")
                return render_template("register.html")

            existing_user = User.query.filter_by(email=email).first()
            if existing_user:
                flash("Este e-mail já está cadastrado.", "danger")
                return render_template("register.html")

            hashed_password = generate_password_hash(password)
            new_user = User(
                email=email,
                password_hash=hashed_password,
                is_active_account=True
            )
            
            db.session.add(new_user)
            db.session.commit()

            flash("Cadastro realizado com sucesso! Faça login.", "success")
            return redirect(url_for("login"))

        return render_template("register.html")
        
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Erro crítico na rota /register")
        flash(f"Erro no cadastro: {str(e)}", "danger")
        return render_template("register.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Você saiu da sessão.", "info")
    return redirect(url_for("login"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)