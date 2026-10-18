import os
import re
import logging
from datetime import datetime, timezone
from functools import wraps

import click
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from flask import Flask, render_template, redirect, url_for, request, jsonify, flash, abort
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user,
)
from flask_wtf.csrf import CSRFProtect
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import generate_password_hash, check_password_hash

from data_source import TwelveDataSource
from extensions import db

load_dotenv()

app = Flask(__name__)

# Produção: defina SECRET_KEY e ENCRYPTION_KEY como variáveis persistentes no Render.
secret_key = os.environ.get("SECRET_KEY", "").strip()
if not secret_key:
    if os.environ.get("RENDER") or os.environ.get("FLASK_ENV") == "production":
        raise RuntimeError("Defina SECRET_KEY nas variáveis de ambiente do serviço.")
    secret_key = "dev-only-change-this-secret-key"
app.config["SECRET_KEY"] = secret_key

app.config["WTF_CSRF_ENABLED"] = False  # Compatibilidade com o painel JSON atual; reative ao incluir tokens CSRF em todos os formulários.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_SAMESITE"] = "Lax"
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

raw_db_url = os.environ.get("DATABASE_URL", "").strip().strip('"').strip("'")
if raw_db_url and "://" in raw_db_url:
    if raw_db_url.startswith("postgres://"):
        raw_db_url = raw_db_url.replace("postgres://", "postgresql://", 1)
    if raw_db_url.startswith("postgresql://"):
        raw_db_url = raw_db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = raw_db_url
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///nct_online.db"

csrf = CSRFProtect(app)
db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Faça login para continuar."
login_manager.login_message_category = "warning"

# A chave deve ser persistente. Nunca gerar uma nova automaticamente em produção,
# pois isso impediria a descriptografia das credenciais já armazenadas.
encryption_key = os.environ.get("ENCRYPTION_KEY", "").strip()
if encryption_key:
    try:
        cipher = Fernet(encryption_key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError("ENCRYPTION_KEY inválida. Gere uma chave Fernet válida.") from exc
else:
    if os.environ.get("RENDER") or os.environ.get("FLASK_ENV") == "production":
        raise RuntimeError("Defina ENCRYPTION_KEY persistente nas variáveis de ambiente.")
    # Desenvolvimento local apenas. Para manter credenciais entre reinícios, configure ENCRYPTION_KEY.
    cipher = Fernet(Fernet.generate_key())
    app.logger.warning("ENCRYPTION_KEY ausente: chave temporária de desenvolvimento em uso.")


def encrypt_secret(value):
    if not value:
        return None
    return cipher.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value):
    if not value:
        return None
    try:
        return cipher.decrypt(value.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        app.logger.warning("Não foi possível descriptografar uma credencial; confira ENCRYPTION_KEY.")
        return None


def mask_secret(value):
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


DEFAULT_PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD", "USD/CAD", "NZD/USD"]
ALLOWED_TIMEFRAMES = {"1min", "5min", "15min", "30min", "1h"}
API_KEY_FIELDS = ["api_key_1", "api_key_2", "api_key_3", "api_key_4"]


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    is_active_account = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    settings = db.relationship("UserSettings", backref="user", uselist=False, cascade="all, delete-orphan")

    def get_id(self):
        return str(self.id)


class UserSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), unique=True, nullable=False)
    timeframe = db.Column(db.String(10), nullable=False, default="5min")
    window_candles = db.Column(db.Integer, nullable=False, default=24)
    leaders = db.Column(db.Integer, nullable=False, default=4)
    followers_per_leader = db.Column(db.Integer, nullable=False, default=6)
    min_similarity = db.Column(db.Float, nullable=False, default=85.0)
    update_seconds = db.Column(db.Integer, nullable=False, default=180)
    closed_candles_only = db.Column(db.Boolean, nullable=False, default=True)
    automatic_pairs = db.Column(db.Boolean, nullable=False, default=True)
    pairs_json = db.Column(db.JSON, nullable=False, default=lambda: list(DEFAULT_PAIRS))
    api_key_1 = db.Column(db.Text, nullable=True)
    api_key_2 = db.Column(db.Text, nullable=True)
    api_key_3 = db.Column(db.Text, nullable=True)
    api_key_4 = db.Column(db.Text, nullable=True)
    analysis_enabled = db.Column(db.Boolean, nullable=False, default=False)
    last_analysis_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_analysis_error = db.Column(db.String(500), nullable=True)
    last_results_json = db.Column(db.JSON, nullable=False, default=dict)


with app.app_context():
    db.create_all()


@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


@app.before_request
def require_active_account():
    if current_user.is_authenticated and not current_user.is_active_account:
        logout_user()
        if request.path.startswith("/api/"):
            return jsonify({"success": False, "error": "Esta conta está bloqueada."}), 403
        flash("Esta conta está bloqueada.", "danger")
        return redirect(url_for("login"))
    return None


def get_settings(user_id=None):
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
        "pairs": settings.pairs_json or list(DEFAULT_PAIRS),
    }


def get_user_api_keys(settings):
    return [decrypt_secret(getattr(settings, name)) for name in API_KEY_FIELDS]


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
    return len(email) <= 254 and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is not None


def api_error(message, status=400):
    return jsonify({"success": False, "error": message}), status


def api_config_response(settings):
    data = settings_to_dict(settings)
    keys_info = []
    for i, field in enumerate(API_KEY_FIELDS, start=1):
        decrypted = decrypt_secret(getattr(settings, field))
        keys_info.append({"number": i, "configured": bool(decrypted), "masked": mask_secret(decrypted)})
    data["api_keys"] = keys_info
    data["api_keys_count"] = sum(1 for item in keys_info if item["configured"])
    return jsonify(data)


def parse_config_numbers(data, settings):
    try:
        values = {
            "window": int(data.get("window_candles", settings.window_candles)),
            "leaders": int(data.get("leaders", settings.leaders)),
            "followers": int(data.get("followers_per_leader", settings.followers_per_leader)),
            "similarity": float(data.get("min_similarity", settings.min_similarity)),
            "update_seconds": int(data.get("update_seconds", settings.update_seconds)),
        }
    except (TypeError, ValueError):
        return None, "Um ou mais parâmetros possuem formato inválido."
    limits = [
        ("window", 5, 200, "A janela deve ficar entre 5 e 200 candles."),
        ("leaders", 1, 10, "A quantidade de líderes deve ficar entre 1 e 10."),
        ("followers", 1, 10, "Seguidores por líder deve ficar entre 1 e 10."),
        ("similarity", 0, 100, "A similaridade deve ficar entre 0 e 100."),
        ("update_seconds", 60, 3600, "O intervalo de atualização deve ficar entre 60 e 3600 segundos."),
    ]
    for name, minimum, maximum, message in limits:
        if not minimum <= values[name] <= maximum:
            return None, message
    return values, None


def normalize_config_pairs(data, settings):
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


def save_api_config(settings, data):
    timeframe = data.get("timeframe", settings.timeframe)
    if timeframe not in ALLOWED_TIMEFRAMES:
        return api_error("Intervalo de análise inválido.")
    numbers, error = parse_config_numbers(data, settings)
    if error:
        return api_error(error)
    pairs, error = normalize_config_pairs(data, settings)
    if error:
        return api_error(error)

    incoming_keys = data.get("api_keys")
    if incoming_keys is not None:
        if not isinstance(incoming_keys, list) or len(incoming_keys) != 4:
            return api_error("Envie exatamente quatro posições para las chaves.")
        for field, value in zip(API_KEY_FIELDS, incoming_keys):
            if value is None or value == "":
                continue  # vazio preserva a chave existente
            if not isinstance(value, str) or not value.strip() or len(value) > 500:
                return api_error("Uma chave informada possui formato inválido.")
            setattr(settings, field, encrypt_secret(value.strip()))

    settings.timeframe = timeframe
    settings.window_candles = numbers["window"]
    settings.leaders = numbers["leaders"]
    settings.followers_per_leader = numbers["followers"]
    settings.min_similarity = numbers["similarity"]
    settings.update_seconds = numbers["update_seconds"]
    settings.closed_candles_only = parse_bool(data.get("closed_candles_only", settings.closed_candles_only), settings.closed_candles_only)
    settings.automatic_pairs = parse_bool(data.get("automatic_pairs", settings.automatic_pairs), settings.automatic_pairs)
    settings.pairs_json = pairs or list(DEFAULT_PAIRS)
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Falha ao salvar configurações.")
        return api_error("Não foi possível salvar as configurações.", 500)
    return jsonify({"success": True, "message": "Configurações salvas.", "config": settings_to_dict(settings)})


def parse_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return bool(default)


@app.route("/")
def index():
    if current_user.is_authenticated:
        return render_template("dashboard.html")
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not valid_email(email):
            flash("Informe um e-mail válido.", "danger")
        elif len(password) < 12:
            flash("Utilize uma senha com pelo menos 12 caracteres.", "danger")
        elif User.query.filter_by(email=email).first():
            flash("Este e-mail já está cadastrado.", "danger")
        else:
            try:
                user = User(email=email, password_hash=generate_password_hash(password))
                db.session.add(user)
                db.session.flush()
                db.session.add(UserSettings(user_id=user.id))
                db.session.commit()
                flash("Conta criada com sucesso! Faça login para continuar.", "success")
                return redirect(url_for("login"))
            except SQLAlchemyError:
                db.session.rollback()
                app.logger.exception("Erro ao criar conta.")
                flash("Não foi possível criar a conta. Tente novamente.", "danger")
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and user.is_active_account and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for("index"))
        flash("E-mail/senha inválidos ou conta bloqueada.", "danger")
    return render_template("login.html")


@app.route("/logout", methods=["GET", "POST"])
@login_required
def logout():
    logout_user()
    flash("Sessão terminada com sucesso.", "info")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/config", methods=["GET", "POST"])
@login_required
def api_config():
    settings = get_settings()
    if request.method == "GET":
        return api_config_response(settings)
    return save_api_config(settings, request.get_json(silent=True) or {})


@app.route("/api/keys/<int:key_num>", methods=["POST"])
@login_required
def save_api_key(key_num):
    if key_num not in range(1, 5):
        return api_error("Número de chave inválido.")
    data = request.get_json(silent=True) or {}
    key_value = data.get("api_key", data.get("key", ""))
    if not isinstance(key_value, str):
        return api_error("Formato de chave inválido.")
    key_value = key_value.strip()
    if not key_value:
        return api_error("Informe uma chave de API válida.")
    if len(key_value) > 500:
        return api_error("A chave informada excede o tamanho permitido.")
    settings = get_settings()
    setattr(settings, API_KEY_FIELDS[key_num - 1], encrypt_secret(key_value))
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Falha ao salvar a chave %s.", key_num)
        return api_error("Não foi possível salvar a chave.", 500)
    return jsonify({"success": True, "message": f"Chave 0{key_num} salva com sucesso!"})


def test_one_key(key_num):
    if key_num not in range(1, 5):
        return api_error("Número de chave inválido.")
    
    settings = get_settings()
    data = request.get_json(silent=True) or {}
    # Permite testar a chave enviada no corpo do request ou a que já está salva
    key = data.get("api_key", data.get("key", "")).strip()
    
    if not key:
        key = get_user_api_keys(settings)[key_num - 1]

    if not key:
        return jsonify({"success": True, "key_number": key_num, "configured": False, "test_success": False, "message": "Chave não configurada ou não informada."})
    
    try:
        result = TwelveDataSource(api_keys=[key]).test_connection()
        if not isinstance(result, dict):
            result = {"success": bool(result), "message": "Teste concluído."}
        return jsonify({"success": True, "key_number": key_num, "configured": True, "test_success": bool(result.get("success")), "masked_key": mask_secret(key), "message": str(result.get("message", "Teste concluído."))})
    except Exception:
        app.logger.exception("Falha ao testar a chave %s.", key_num)
        return jsonify({"success": True, "key_number": key_num, "configured": True, "test_success": False, "masked_key": mask_secret(key), "message": "Falha no teste de conexão. Confira a chave e tente novamente."})


@app.route("/api/keys/test/<int:key_num>", methods=["POST"])
@login_required
def test_api_key(key_num):
    return test_one_key(key_num)


@app.route("/api/keys/test-all", methods=["POST"])
@login_required
def test_all_api_keys():
    settings = get_settings()
    keys = get_user_api_keys(settings)
    results = []
    for number, key in enumerate(keys, start=1):
        if not key:
            results.append({"key_number": number, "configured": False, "success": False, "message": "Chave não configurada ou não pôde ser descriptografada."})
            continue
        try:
            result = TwelveDataSource(api_keys=[key]).test_connection()
            if not isinstance(result, dict):
                result = {"success": bool(result), "message": "Teste concluído."}
            results.append({"key_number": number, "configured": True, "success": bool(result.get("success")), "masked_key": mask_secret(key), "message": str(result.get("message", "Teste concluído."))})
        except Exception:
            app.logger.exception("Falha ao testar a chave %s.", number)
            results.append({"key_number": number, "configured": True, "success": False, "masked_key": mask_secret(key), "message": "Falha no teste de conexão."})
    return jsonify({"success": True, "results": results})


@app.route("/api/keys/<int:key_num>", methods=["DELETE"])
@login_required
def delete_api_key(key_num):
    if key_num not in range(1, 5):
        return api_error("Número de chave inválido.")
    settings = get_settings()
    setattr(settings, API_KEY_FIELDS[key_num - 1], None)
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Falha ao remover a chave %s.", key_num)
        return api_error("Não foi possível remover a chave.", 500)
    return jsonify({"success": True, "message": f"Chave 0{key_num} removida."})


@app.route("/api/toggle", methods=["POST"])
@login_required
def api_toggle():
    settings = get_settings()
    data = request.get_json(silent=True) or {}
    requested = data.get("enabled")
    if not isinstance(requested, bool):
        return api_error("Informe enabled como verdadeiro ou falso.")
    keys = get_user_api_keys(settings)
    if requested and not any(keys):
        return api_error("Configure pelo menos uma chave de API válida antes de iniciar.")
    settings.analysis_enabled = requested
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Falha ao alterar estado da análise.")
        return api_error("Não foi possível alterar o estado da análise.", 500)
    return jsonify({"success": True, "enabled": settings.analysis_enabled, "message": "Estado atualizado."})


@app.route("/api/state")
@login_required
def api_state():
    settings = get_settings()
    config = settings_to_dict(settings)
    config["api_keys_count"] = sum(bool(key) for key in get_user_api_keys(settings))
    return jsonify({
        "success": True,
        "user": {"email": current_user.email, "is_admin": current_user.is_admin},
        "config": config,
        "analysis_enabled": settings.analysis_enabled,
        "last_analysis_at": settings.last_analysis_at.isoformat() if settings.last_analysis_at else None,
        "last_analysis_error": settings.last_analysis_error,
        "results": settings.last_results_json or {},
        "last_results_json": settings.last_results_json or {},
    })


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
    active = (request.get_json(silent=True) or {}).get("active")
    if not isinstance(active, bool):
        return api_error("Informe active como verdadeiro ou falso.")
    if user.id == current_user.id and not active:
        return api_error("Você não pode bloquear sua própria conta.")
    user.is_active_account = active
    if not active and user.settings:
        user.settings.analysis_enabled = False
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Falha ao alterar conta do usuário %s.", user_id)
        return api_error("Não foi possível alterar a conta.", 500)
    return jsonify({"success": True})


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_user_delete(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    if user.id == current_user.id:
        return api_error("Você não pode excluir sua própria conta.")
    try:
        db.session.delete(user)
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Falha ao excluir conta do usuário %s.", user_id)
        return api_error("Não foi possível excluir a conta.", 500)
    return jsonify({"success": True})


@app.cli.command("init-db")
def init_db():
    with app.app_context():
        db.create_all()
    click.echo("Tabelas criadas/verificadas.")


@app.cli.command("criar-admin")
@click.option("--email", prompt="E-mail do Admin", help="E-mail para a conta admin")
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True, help="Senha para a conta admin")
def criar_admin(email, password):
    email = email.strip().lower()
    if not valid_email(email):
        raise click.ClickException("E-mail inválido.")
    if len(password) < 12:
        raise click.ClickException("Use uma senha com pelo menos 12 caracteres.")
    try:
        user = User.query.filter_by(email=email).first()
        if user is None:
            user = User(email=email, password_hash=generate_password_hash(password), is_admin=True, is_active_account=True)
            db.session.add(user)
            db.session.flush()
            db.session.add(UserSettings(user_id=user.id))
        else:
            user.password_hash = generate_password_hash(password)
            user.is_admin = True
            user.is_active_account = True
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Falha ao criar/atualizar administrador.")
        raise click.ClickException("Não foi possível criar/atualizar a conta administrativa.")
    click.echo("Conta administrativa criada/atualizada com sucesso.")


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


@app.errorhandler(Exception)
def handle_exception(error):
    # Log interno para diagnóstico; nunca enviar traceback ao navegador.
    app.logger.exception("Erro não tratado na aplicação: %s", error)
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "error": "Erro interno. Tente novamente."}), 500
    return "Erro interno. Tente novamente.", 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)