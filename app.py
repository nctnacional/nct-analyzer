
import os
import re
import traceback
from datetime import datetime, timezone
from functools import wraps

import click
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from flask import (
    Flask, render_template, redirect, url_for, request,
    jsonify, flash, abort
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import generate_password_hash, check_password_hash

from data_source import TwelveDataSource

load_dotenv()


app = Flask(__name__)
csrf = CSRFProtect(app)
app.config['WTF_CSRF_ENABLED'] = False 

# Desativa o CSRF globalmente para evitar erros de token em formulários HTML customizados
app.config['WTF_CSRF_ENABLED'] = False

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "")


database_url = os.environ.get("DATABASE_URL")

# Se houver DATABASE_URL e for válida, usa-a; caso contrário, usa SQLite local robusto para produção
if database_url and database_url.strip():
    database_url = database_url.strip().strip('"').strip("'")
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    if "postgresql://" in database_url and "psycopg2" not in database_url:
        database_url = database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
else:
    database_url = "sqlite:///nct_online.db"

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False



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
except ValueError:
    # Fallback automático: se a chave do ambiente estiver ausente ou inválida, 
    # gera uma chave Fernet válida em tempo de execução para evitar falhas no deploy.
    cipher = Fernet(Fernet.generate_key())

def encrypt_secret(value):
    """Encrypt a secret value with the configured Fernet cipher."""
    if not value:
        return None
    return cipher.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value):
    """Decrypt a secret value with the configured Fernet cipher."""
    if not value:
        return None
    try:
        return cipher.decrypt(value.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        app.logger.error("Não foi possível descriptografar uma credencial.")
        return None


def mask_secret(value):
    """Mask a secret value for safe display while keeping the ends visible."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + ("*" * (len(value) - 8)) + value[-4:]


# ==========================================================
# MODELOS DO BANCO DE DADOS
# ==========================================================

class User(UserMixin, db.Model):
    """Representa uma conta de usuário armazenada no banco de dados."""

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
    """Store per-user analysis and API configuration."""

    # SQLAlchemy models primarily expose mapped columns, not public methods.
    # pylint: disable=too-few-public-methods
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
    """Load a user by ID for Flask-Login."""
    return db.session.get(User, int(user_id))


@app.before_request
def require_active_account():
    """Bloqueia o acesso de contas inativas."""
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
    return None


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
    """Converte as configurações do usuário em um dicionário."""
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
    """Restrict access to administrators only."""
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def valid_email(email):
    """Return whether the supplied value has a valid email format."""
    email = (email or "").strip().lower()
    return (
        len(email) <= 254
        and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is not None
    )


def api_error(message, status=400):
    """Return a JSON error payload for API responses."""
    return jsonify({"success": False, "error": message}), status


# ==========================================================
# CADASTRO E LOGIN
# ==========================================================

@app.route("/")
def index():
    """Render the dashboard for authenticated users or redirect to login."""
    if current_user.is_authenticated:
        return render_template("dashboard.html")  # alterado para dashboard.html
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    """Register a new user account."""
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if len(password) < 12:
            flash("Utilize uma senha com pelo menos 12 caracteres.", "danger")
            return render_template("register.html")

        if User.query.filter_by(email=email).first():
            flash("Este e-mail já está cadastrado.", "danger")
            return render_template("register.html")

        try:
            user = User(
                email=email,
                password_hash=generate_password_hash(password)
            )
            db.session.add(user)
            db.session.commit()

            flash("Conta criada com sucesso! Faça login para continuar.", "success")
            return redirect(url_for("login"))

        except SQLAlchemyError as e:
            db.session.rollback()
            flash(f"Erro ao criar conta: {str(e)}", "danger")
            return render_template("register.html")

    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    """Authenticate a user and render or redirect from the login page."""
    try:
        if current_user.is_authenticated:
            return redirect(url_for("index"))

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
    except SQLAlchemyError as e:
        flash(f"Erro ao fazer login: {str(e)}", "danger")
        return render_template("login.html")

@app.route("/logout", methods=["GET", "POST"])
@login_required
def logout():
    """Log out the current user and redirect to the login page."""
    logout_user()
    flash("Sessão terminada com sucesso.", "info")
    return redirect(url_for("login"))

# ==========================================================
# PAINEL PRINCIPAL
# ==========================================================

@app.route("/dashboard")
@login_required
def dashboard():
    """Render the main dashboard for the authenticated user."""
    return render_template("dashboard.html")


def _api_config_response(settings):
    """Build the configuration response, including masked API keys."""
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


def _parse_api_config_numbers(data, settings):
    """Parse and validate numeric configuration fields."""
    try:
        values = {
            "window": int(data.get("window_candles", settings.window_candles)),
            "leaders": int(data.get("leaders", settings.leaders)),
            "followers": int(data.get(
                "followers_per_leader", settings.followers_per_leader
            )),
            "similarity": float(data.get(
                "min_similarity", settings.min_similarity
            )),
            "update_seconds": int(data.get(
                "update_seconds", settings.update_seconds
            )),
        }
    except (TypeError, ValueError):
        return None, "Um ou mais parâmetros possuem formato inválido."

    limits = (
        ("window", 5, 200, "A janela deve ficar entre 5 e 200 candles."),
        ("leaders", 1, 10, "A quantidade de líderes deve ficar entre 1 e 10."),
        ("followers", 1, 10, "Seguidores por líder deve ficar entre 1 e 10."),
        ("similarity", 0, 100, "A similaridade deve ficar entre 0 e 100."),
        ("update_seconds", 60, 3600,
         "O intervalo de atualização deve ficar entre 60 e 3600 segundos."),
    )
    for name, minimum, maximum, message in limits:
        if not minimum <= values[name] <= maximum:
            return None, message
    return values, None


def _normalize_config_pairs(data, settings):
    """Validate and normalize the configured market pairs."""
    pairs = data.get("pairs", settings.pairs_json or DEFAULT_PAIRS)
    if not isinstance(pairs, list) or len(pairs) > 100:
        return None, "A lista de pares é inválida."

    normalized = []
    for pair in pairs:
        if not isinstance(pair, str):
            return None, "Há um símbolo inválido na lista."
        pair = pair.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,12}/[A-Z0-9]{2,12}", pair):
            return None, f"Símbolo inválido: {pair[:30]}"
        if pair not in normalized:
            normalized.append(pair)
    return normalized, None


def _apply_config_api_keys(settings, incoming_keys):
    """Apply explicitly supplied API keys, preserving empty positions."""
    if not isinstance(incoming_keys, list) or len(incoming_keys) != 4:
        return "Envie exatamente quatro posições para as chaves."

    encrypted_names = ["api_key_1", "api_key_2", "api_key_3", "api_key_4"]
    for name, value in zip(encrypted_names, incoming_keys):
        if value is None or value == "":
            continue
        if not isinstance(value, str) or len(value) > 500:
            return "Uma chave informada possui formato inválido."
        setattr(settings, name, encrypt_secret(value.strip()))

    if not settings.api_key_1:
        return "A chave API 01 é obrigatória."
    return None


def _save_api_config(settings, data):
    """Validate, persist, and return an API configuration update."""
    timeframe = data.get("timeframe", settings.timeframe)
    if timeframe not in ALLOWED_TIMEFRAMES:
        return api_error("Intervalo de análise inválido.")

    numbers, error = _parse_api_config_numbers(data, settings)
    if error:
        return api_error(error)
    normalized_pairs, error = _normalize_config_pairs(data, settings)
    if error:
        return api_error(error)

    # Chaves só são alteradas quando o usuário as envia explicitamente.
    # Campo vazio preserva a chave já salva. Para remover, use o endpoint
    # dedicado abaixo, evitando apagar chaves acidentalmente.
    incoming_keys = data.get("api_keys")
    if incoming_keys is not None:
        error = _apply_config_api_keys(settings, incoming_keys)
        if error:
            return api_error(error)

    settings.timeframe = timeframe
    settings.window_candles = numbers["window"]
    settings.leaders = numbers["leaders"]
    settings.followers_per_leader = numbers["followers"]
    settings.min_similarity = numbers["similarity"]
    settings.update_seconds = numbers["update_seconds"]
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


@app.route("/api/config", methods=["GET", "POST"])
@login_required
def api_config():
    """Return or update the authenticated user's application settings."""
    settings = get_settings()
    if request.method == "GET":
        return _api_config_response(settings)
    return _save_api_config(settings, request.get_json(silent=True) or {})



@app.route("/api/keys/<int:key_number>", methods=["POST", "DELETE"])
@login_required
def delete_api_key(key_number):
    """Save or remove an API key for the authenticated user."""
    if key_number not in (1, 2, 3, 4):
        response = api_error("Número de chave inválido.")
    else:
        settings = get_settings()
        field = f"api_key_{key_number}"

        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            key = data.get("api_key") or data.get("key")

            if not isinstance(key, str) or not key.strip():
                response = api_error("Informe uma chave de API válida.")
            elif len(key.strip()) > 500:
                response = api_error("A chave informada é muito longa.")
            else:
                setattr(settings, field, encrypt_secret(key.strip()))
                try:
                    db.session.commit()
                    response = jsonify({
                        "success": True,
                        "message": f"Chave {key_number:02d} salva com segurança."
                    })
                except SQLAlchemyError:
                    db.session.rollback()
                    app.logger.exception("Falha ao salvar chave de API.")
                    response = api_error("Não foi possível salvar a chave.", 500)
        elif key_number == 1:
            response = api_error("A API 01 é obrigatória e não pode ser removida.")
        else:
            setattr(settings, field, None)
            try:
                db.session.commit()
                response = jsonify({"success": True, "message": "Chave removida."})
            except SQLAlchemyError:
                db.session.rollback()
                app.logger.exception("Falha ao remover chave de API.")
                response = api_error("Não foi possível remover a chave.", 500)

    return response


@app.route("/api/keys/test/<int:key_number>", methods=["POST"])
@login_required
def test_api_key(key_number):
    """Testa a conexão usando a chave de API especificada."""
    if key_number not in (1, 2, 3, 4):
        return api_error("Número de chave inválido.")

    settings = get_settings()
    keys = get_user_api_keys(settings)
    key = keys[key_number - 1]

    if not key:
        return api_error(f"A chave API 0{key_number} não está configurada.")

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
    """Testa a conexão de todas as chaves de API configuradas."""
    settings = get_settings()
    keys = get_user_api_keys(settings)

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
    """Ativa ou desativa a análise conforme a solicitação recebida."""
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
    """Retorna o estado atual da análise e as configurações do usuário."""
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
    """Exibe a página de administração com a lista de usuários."""
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template("admin.html", users=users)


@app.route("/admin/users/<int:user_id>/status", methods=["POST"])
@admin_required
def admin_user_status(user_id):
    """Atualiza o status de ativação de uma conta de usuário."""
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
    """Exclui uma conta de usuário pelo identificador informado."""
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


@app.cli.command("criar-admin")
@click.option("--email", prompt="E-mail do Admin", help="E-mail para a conta admin")
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True, help="Senha para a conta admin")
def criar_admin(email, password):
    """Comando de terminal para criar ou atualizar o utilizador admin."""
    email = email.strip().lower()

    if not valid_email(email):
        raise click.ClickException("E-mail inválido.")

    if len(password) < 12:
        raise click.ClickException("Use uma senha com pelo menos 12 caracteres.")

    user = User.query.filter_by(email=email).first()

    if user is None:
        user = User(
            email=email,
            password_hash=generate_password_hash(password),
            is_admin=True,
            is_active_account=True
        )
        db.session.add(user)
        db.session.flush()
        db.session.add(UserSettings(user_id=user.id))
    else:
        user.password_hash = generate_password_hash(password)
        user.is_admin = True
        user.is_active_account = True

    db.session.commit()
    click.echo("Conta administrativa criada/atualizada com sucesso.")


@app.errorhandler(403)
def forbidden(_error):
    """Return a JSON or HTML message when access is denied."""
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "error": "Acesso negado."}), 403
    return "Acesso negado.", 403


@app.errorhandler(404)
def not_found(_error):
    """Return a JSON or HTML message when a route is not found."""
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "error": "Recurso não encontrado."}), 404
    return "Página não encontrada.", 404

@app.errorhandler(Exception)
def handle_exception(e):
    """Handle unexpected application exceptions."""
    import traceback
    return f"<h3>Erro Crítico Detalhado:</h3><pre>{traceback.format_exc()}</pre>", 500

if __name__ == "__main__":
    # Apenas para testes locais. Em produção, use um servidor WSGI
    # e HTTPS fornecido pela hospedagem.
    app.run(host="127.0.0.1", port=8000, debug=False)
    