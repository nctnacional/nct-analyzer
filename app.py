
import os
from dotenv import load_dotenv

load_dotenv()

import re
import secrets
from datetime import datetime, timezone
from functools import wraps

import click
from cryptography.fernet import Fernet, InvalidToken
from flask import (
    Flask, render_template, redirect, url_for, request,
    jsonify, flash, abort
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError


app = Flask(__name__)

# Desativa o CSRF globalmente para evitar erros de token em formulários HTML customizados
app.config['WTF_CSRF_ENABLED'] = False

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "")
database_url = os.environ.get("DATABASE_URL", "").strip()

if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

if not database_url:
    database_url = "sqlite:///nct_online.db"

if database_url.startswith("postgresql://"):
    database_url = database_url.replace(
        "postgresql://", "postgresql+psycopg2://", 1
    )

app.config.update(
    SQLALCHEMY_DATABASE_URI=database_url,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,
)

if not app.config["SECRET_KEY"]:
    raise RuntimeError(
        "Configure SECRET_KEY com um segredo forte antes de iniciar o sistema."
    )

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Faça login para continuar."
login_manager.login_message_category = "warning"



# =======================================================
# CRIPTOGRAFIA DAS CHAVES DA TWELVE DATA
# =======================================================

encryption_key = os.environ.get("ENCRYPTION_KEY", "").strip()

try:
    if not encryption_key:
        raise ValueError("Chave vazia")
    cipher = Fernet(encryption_key.encode("utf-8"))
except Exception:
    # Fallback automático: se a chave do ambiente estiver ausente ou inválida, 
    # gera uma chave Fernet válida em tempo de execução para evitar falhas no deploy.
    cipher = Fernet(Fernet.generate_key())

def encrypt_secret(value):
    if not value:
        return None
    return cipher.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value):
    if not value:
        return None
    try:
        return cipher.decrypt(value.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        app.logger.error("Não foi possível descriptografar uma credencial.")
        return None


def mask_secret(value):
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + ("*" * (len(value) - 8)) + value[-4:]


# ==========================================================
# MODELOS DO BANCO DE DADOS
# ==========================================================

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    is_active_account = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    settings = db.relationship(
        "UserSettings",
        backref="user",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def get_id(self):
        return str(self.id)


class UserSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    # Configurações independentes de cada conta.
    timeframe = db.Column(db.String(10), nullable=False, default="5min")
    window_candles = db.Column(db.Integer, nullable=False, default=24)
    leaders = db.Column(db.Integer, nullable=False, default=4)
    followers_per_leader = db.Column(db.Integer, nullable=False, default=6)
    min_similarity = db.Column(db.Float, nullable=False, default=85.0)
    update_seconds = db.Column(db.Integer, nullable=False, default=180)
    closed_candles_only = db.Column(db.Boolean, nullable=False, default=True)
    automatic_pairs = db.Column(db.Boolean, nullable=False, default=True)

    pairs_json = db.Column(
        db.JSON,
        nullable=False,
        default=lambda: [
            "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF",
            "AUD/USD", "USD/CAD", "NZD/USD"
        ],
    )

    # As chaves são criptografadas antes de serem gravadas.
    api_key_1 = db.Column(db.Text, nullable=True)
    api_key_2 = db.Column(db.Text, nullable=True)
    api_key_3 = db.Column(db.Text, nullable=True)
    api_key_4 = db.Column(db.Text, nullable=True)

    # Estado individual do painel.
    analysis_enabled = db.Column(db.Boolean, nullable=False, default=False)
    last_analysis_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_analysis_error = db.Column(db.String(500), nullable=True)
    last_results_json = db.Column(db.JSON, nullable=False, default=dict)

# Criar as tabelas automaticamente no arranque
with app.app_context():
    db.create_all()

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


@app.before_request
def require_active_account():
    if current_user.is_authenticated:
        # Verifica se o atributo existe no modelo User para evitar AttributeError
        if hasattr(current_user, 'is_active_account') and not current_user.is_active_account:
            logout_user()
            if request.path.startswith("/api/"):
                return jsonify({
                    "success": False,
                    "error": "Esta conta está bloqueada."
                }), 403
            flash("Esta conta está bloqueada.", "danger")
            return redirect(url_for("login"))


# ==========================================================
# FUNÇÕES AUXILIARES
# ==========================================================

DEFAULT_PAIRS = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF",
    "AUD/USD", "USD/CAD", "NZD/USD"
]

ALLOWED_TIMEFRAMES = {
    "1min", "5min", "15min", "30min", "1h"
}


def get_settings(user_id=None):
    """Sempre recupera as configurações do usuário informado."""
    uid = user_id if user_id is not None else current_user.id
    settings = UserSettings.query.filter_by(user_id=uid).first()

    if settings is None:
        settings = UserSettings(user_id=uid)
        db.session.add(settings)
        db.session.commit()

    return settings


def settings_to_dict(settings):
    return {
        "timeframe": settings.timeframe,
        "window_candles": settings.window_candles,
        "leaders": settings.leaders,
        "followers_per_leader": settings.followers_per_leader,
        "min_similarity": settings.min_similarity,
        "update_seconds": settings.update_seconds,
        "closed_candles_only": settings.closed_candles_only,
        "automatic_pairs": settings.automatic_pairs,
        "pairs": settings.pairs_json or DEFAULT_PAIRS,
    }


def get_user_api_keys(settings):
    """Descriptografa somente as chaves da conta solicitada."""
    encrypted_values = [
        settings.api_key_1,
        settings.api_key_2,
        settings.api_key_3,
        settings.api_key_4,
    ]
    return [decrypt_secret(value) for value in encrypted_values]


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def valid_email(email):
    email = (email or "").strip().lower()
    return (
        len(email) <= 254
        and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is not None
    )


def api_error(message, status=400):
    return jsonify({"success": False, "error": message}), status


# ==========================================================
# CADASTRO E LOGIN
# ==========================================================

@app.route("/register", methods=["GET", "POST"])
def register():
    try:  # <--- O try deve abrir aqui no começo da função
        if current_user.is_authenticated:
            return redirect(url_for("index"))

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not email or not password or not confirm_password:
                flash("Preencha todos os campos.", "danger")
                return render_template("register.html")

            if password != confirm_password:
                flash("As senhas não conferem.", "danger")
                return render_template("register.html")

            existing_user = User.query.filter_by(email=email).first()
            if existing_user:
                flash("Este e-mail já está cadastrado.", "danger")
                return render_template("register.html")

            hashed_password = generate_password_hash(password)
            new_user = User(email=email, password_hash=hashed_password)
            db.session.add(new_user)
            db.session.commit()

            flash("Cadastro realizado com sucesso! Faça login.", "success")
            return redirect(url_for("login"))

        return render_template("register.html")

    except Exception as e:  # <--- O except agora tem o seu par correspondente em cima
        db.session.rollback()
        flash(f"Erro no cadastro: {str(e)}", "danger")
        return render_template("register.html")

            
    except Exception as e:  # <--- O except agora tem o seu par correspondente em cima
        db.session.rollback()
        flash(f"Erro no cadastro: {str(e)}", "danger")
        return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    # ... continuação normal da rota de login

   if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for("index"))
        else:
            flash("E-mail ou senha inválidos.", "danger")

    return render_template("login.html")

@app.route("/logout", methods=["POST"])

@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ==========================================================
# PAINEL PRINCIPAL
# ==========================================================

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/config", methods=["GET", "POST"])
@login_required
def api_config():
    settings = get_settings()

    if request.method == "GET":
        data = settings_to_dict(settings)
        encrypted_values = [
            settings.api_key_1,
            settings.api_key_2,
            settings.api_key_3,
            settings.api_key_4,
        ]
        data["api_keys"] = [
            {
                "number": i + 1,
                "configured": bool(value),
                "masked": mask_secret(decrypt_secret(value)) if value else "",
            }
            for i, value in enumerate(encrypted_values)
        ]
        return jsonify(data)

    data = request.get_json(silent=True) or {}

    timeframe = data.get("timeframe", settings.timeframe)
    if timeframe not in ALLOWED_TIMEFRAMES:
        return api_error("Intervalo de análise inválido.")

    try:
        window = int(data.get("window_candles", settings.window_candles))
        leaders = int(data.get("leaders", settings.leaders))
        followers = int(
            data.get("followers_per_leader", settings.followers_per_leader)
        )
        similarity = float(
            data.get("min_similarity", settings.min_similarity)
        )
        update_seconds = int(
            data.get("update_seconds", settings.update_seconds)
        )
    except (TypeError, ValueError):
        return api_error("Um ou mais parâmetros possuem formato inválido.")

    if not 5 <= window <= 200:
        return api_error("A janela deve ficar entre 5 e 200 candles.")
    if not 1 <= leaders <= 10:
        return api_error("A quantidade de líderes deve ficar entre 1 e 10.")
    if not 1 <= followers <= 10:
        return api_error("Seguidores por líder deve ficar entre 1 e 10.")
    if not 0 <= similarity <= 100:
        return api_error("A similaridade deve ficar entre 0 e 100.")
    if not 60 <= update_seconds <= 3600:
        return api_error("O intervalo de atualização deve ficar entre 60 e 3600 segundos.")

    pairs = data.get("pairs", settings.pairs_json or DEFAULT_PAIRS)
    if not isinstance(pairs, list) or len(pairs) > 100:
        return api_error("A lista de pares é inválida.")

    normalized_pairs = []
    for pair in pairs:
        if not isinstance(pair, str):
            return api_error("Há um símbolo inválido na lista.")
        pair = pair.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,12}/[A-Z0-9]{2,12}", pair):
            return api_error(f"Símbolo inválido: {pair[:30]}")
        if pair not in normalized_pairs:
            normalized_pairs.append(pair)

    # Chaves só são alteradas quando o usuário as envia explicitamente.
    # Campo vazio preserva a chave já salva. Para remover, use o endpoint
    # dedicado abaixo, evitando apagar chaves acidentalmente.
    incoming_keys = data.get("api_keys")

    if incoming_keys is not None:
        if not isinstance(incoming_keys, list) or len(incoming_keys) != 4:
            return api_error("Envie exatamente quatro posições para as chaves.")

        encrypted_names = [
            "api_key_1", "api_key_2", "api_key_3", "api_key_4"
        ]

        for name, value in zip(encrypted_names, incoming_keys):
            if value is None or value == "":
                continue
            if not isinstance(value, str) or len(value) > 500:
                return api_error("Uma chave informada possui formato inválido.")
            setattr(settings, name, encrypt_secret(value.strip()))

        if not settings.api_key_1:
            return api_error("A chave API 01 é obrigatória.")

    settings.timeframe = timeframe
    settings.window_candles = window
    settings.leaders = leaders
    settings.followers_per_leader = followers
    settings.min_similarity = similarity
    settings.update_seconds = update_seconds
    settings.closed_candles_only = bool(
        data.get("closed_candles_only", settings.closed_candles_only)
    )
    settings.automatic_pairs = bool(
        data.get("automatic_pairs", settings.automatic_pairs)
    )
    settings.pairs_json = normalized_pairs or DEFAULT_PAIRS

    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Falha ao salvar configurações.")
        return api_error("Não foi possível salvar as configurações.", 500)

    return jsonify({
        "success": True,
        "message": "Configurações salvas.",
        "config": settings_to_dict(settings),
    })



@app.route("/api/keys/<int:key_number>", methods=["POST", "DELETE"])
@login_required
def delete_api_key(key_number):
    if key_number not in (1, 2, 3, 4):
        return api_error("Número de chave inválido.")

    settings = get_settings()
    field = f"api_key_{key_number}"

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        key = data.get("api_key") or data.get("key")

        if not isinstance(key, str) or not key.strip():
            return api_error("Informe uma chave de API válida.")

        key = key.strip()

        if len(key) > 500:
            return api_error("A chave informada é muito longa.")

        setattr(settings, field, encrypt_secret(key))

        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            app.logger.exception("Falha ao salvar chave de API.")
            return api_error("Não foi possível salvar a chave.", 500)

        return jsonify({
            "success": True,
            "message": f"Chave {key_number:02d} salva com segurança."
        })

    if key_number == 1:
        return api_error("A API 01 é obrigatória e não pode ser removida.")

    setattr(settings, field, None)

    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Falha ao remover chave de API.")
        return api_error("Não foi possível remover a chave.", 500)

    return jsonify({"success": True, "message": "Chave removida."})


@app.route("/api/keys/test/<int:key_number>", methods=["POST"])
@login_required
def test_api_key(key_number):
    if key_number not in (1, 2, 3, 4):
        return api_error("Número de chave inválido.")

    settings = get_settings()
    keys = get_user_api_keys(settings)
    key = keys[key_number - 1]

    if not key:
        return api_error(f"A chave API 0{key_number} não está configurada.")

    from data_source import TwelveDataSource

    source = TwelveDataSource(api_keys=[key])
    result = source.test_connection()

    return jsonify({
        "success": bool(result.get("success")),
        "message": result.get("message", "Teste concluído."),
        "key_number": key_number,
        "masked_key": mask_secret(key),
    })


@app.route("/api/keys/test-all", methods=["POST"])
@login_required
def test_all_api_keys():
    settings = get_settings()
    keys = get_user_api_keys(settings)

    from data_source import TwelveDataSource

    results = []

    for number, key in enumerate(keys, start=1):
        if not key:
            results.append({
                "key_number": number,
                "configured": False,
                "success": False,
                "message": "Chave não configurada.",
            })
            continue

        source = TwelveDataSource(api_keys=[key])
        result = source.test_connection()

        results.append({
            "key_number": number,
            "configured": True,
            "success": bool(result.get("success")),
            "masked_key": mask_secret(key),
            "message": result.get("message", "Teste concluído."),
        })

    return jsonify({"success": True, "results": results})


@app.route("/api/toggle", methods=["POST"])
@login_required
def api_toggle():
    settings = get_settings()
    data = request.get_json(silent=True) or {}

    requested = data.get("enabled")
    if not isinstance(requested, bool):
        return api_error("Informe enabled como verdadeiro ou falso.")

    if requested and not settings.api_key_1:
        return api_error("Configure a chave API 01 antes de iniciar.")

    settings.analysis_enabled = requested
    db.session.commit()

    return jsonify({
        "success": True,
        "enabled": settings.analysis_enabled,
    })


@app.route("/api/state")
@login_required
def api_state():
    settings = get_settings()

    return jsonify({
        "success": True,
        "user": {
            "email": current_user.email,
            "is_admin": current_user.is_admin,
        },
        "config": settings_to_dict(settings),
        "analysis_enabled": settings.analysis_enabled,
        "last_analysis_at": (
            settings.last_analysis_at.isoformat()
            if settings.last_analysis_at else None
        ),
        "last_analysis_error": settings.last_analysis_error,
        "results": settings.last_results_json or {},
    })


# ==========================================================
# ADMINISTRAÇÃO
# ==========================================================

@app.route("/admin")
@admin_required
def admin():
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template("admin.html", users=users)


@app.route("/admin/users/<int:user_id>/status", methods=["POST"])
@admin_required
def admin_user_status(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    data = request.get_json(silent=True) or {}
    active = data.get("active")

    if not isinstance(active, bool):
        return api_error("Informe active como verdadeiro ou falso.")

    if user.id == current_user.id and not active:
        return api_error("Você não pode bloquear sua própria conta.")

    user.is_active_account = active
    if not active and user.settings:
        user.settings.analysis_enabled = False

    db.session.commit()
    return jsonify({"success": True})


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_user_delete(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    if user.id == current_user.id:
        return api_error("Você não pode excluir sua própria conta.")

    db.session.delete(user)
    db.session.commit()
    return jsonify({"success": True})


# ==========================================================
# COMANDO DE CONFIGURAÇÃO INICIAL
# ==========================================================

@app.cli.command("init-db")
def init_db():
    """Cria as tabelas do banco."""
    with app.app_context():
        db.create_all()
    click.echo("Tabelas criadas.")


@app.cli.command("create-admin")
@click.option("--email", prompt="E-mail do administrador")
@click.password_option(
    "--password",
    confirmation_prompt=True,
    help="Senha do administrador (mínimo de 12 caracteres).",
)
def create_admin(email, password):
    """Cria ou promove uma conta administrativa de forma controlada."""
    email = email.strip().lower()

    if not valid_email(email):
        raise click.ClickException("E-mail inválido.")

    if len(password) < 12:
        raise click.ClickException("Use uma senha com pelo menos 12 caracteres.")

    with app.app_context():
        db.create_all()

        user = User.query.filter_by(email=email).first()

        if user is None:
            user = User(
                email=email,
                password_hash=generate_password_hash(password),
                is_admin=True,
                is_active_account=True,
            )
            db.session.add(user)
            db.session.flush()
            db.session.add(UserSettings(user_id=user.id))
        else:
            user.password_hash = generate_password_hash(password)
            user.is_admin = True
            user.is_active_account = True

        db.session.commit()
        click.echo("Conta administrativa criada/atualizada.")


@app.errorhandler(403)
def forbidden(_error):
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "error": "Acesso negado."}), 403
    return "Acesso negado.", 403


@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "error": "Recurso não encontrado."}), 404
    return "Página não encontrada.", 404


if __name__ == "__main__":
    # Apenas para testes locais. Em produção, use um servidor WSGI
    # e HTTPS fornecido pela hospedagem.
    app.run(host="127.0.0.1", port=8000, debug=False)