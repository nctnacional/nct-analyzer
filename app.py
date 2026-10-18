import os
import re
import logging
import threading
import time
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
from analyzer import analyze_market
from extensions import db

load_dotenv()

app = Flask(__name__)

secret_key = os.environ.get("SECRET_KEY", "").strip()
if not secret_key:
    if os.environ.get("RENDER") or os.environ.get("FLASK_ENV") == "production":
        raise RuntimeError("Defina SECRET_KEY nas variáveis de ambiente do serviço.")
    secret_key = "dev-only-change-this-secret-key"
app.config["SECRET_KEY"] = secret_key

app.config["WTF_CSRF_ENABLED"] = False
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

encryption_key = os.environ.get("ENCRYPTION_KEY", "").strip()
if encryption_key:
    try:
        cipher = Fernet(encryption_key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError("ENCRYPTION_KEY inválida. Gere uma chave Fernet válida.") from exc
else:
    if os.environ.get("RENDER") or os.environ.get("FLASK_ENV") == "production":
        raise RuntimeError("Defina ENCRYPTION_KEY persistente nas variáveis de ambiente.")
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


@app.route('/api/config', methods=['GET', 'POST'])
@login_required
def api_config():
    user_settings = UserSettings.query.filter_by(user_id=current_user.id).first()
    if not user_settings:
        user_settings = UserSettings(user_id=current_user.id)
        db.session.add(user_settings)
        db.session.commit()

    if request.method == 'POST':
        data = request.get_json() or {}
        user_settings.timeframe = data.get('timeframe', user_settings.timeframe)
        user_settings.window_candles = int(data.get('window_candles', user_settings.window_candles))
        user_settings.leaders = int(data.get('leaders', user_settings.leaders))
        user_settings.followers_per_leader = int(data.get('followers_per_leader', user_settings.followers_per_leader))
        user_settings.min_similarity = float(data.get('min_similarity', user_settings.min_similarity))
        user_settings.update_seconds = int(data.get('update_seconds', user_settings.update_seconds))
        user_settings.closed_candles_only = bool(data.get('closed_candles_only', user_settings.closed_candles_only))
        user_settings.automatic_pairs = bool(data.get('automatic_pairs', user_settings.automatic_pairs))

        db.session.commit()
        return jsonify({'success': True, 'message': 'Configurações salvas com sucesso!'})

    keys_count = sum([
        1 for k in [user_settings.api_key_1, user_settings.api_key_2, user_settings.api_key_3, user_settings.api_key_4] if k
    ])

    return jsonify({
        'success': True,
        'config': {
            'timeframe': user_settings.timeframe,
            'window_candles': user_settings.window_candles,
            'leaders': user_settings.leaders,
            'followers_per_leader': user_settings.followers_per_leader,
            'min_similarity': user_settings.min_similarity,
            'update_seconds': user_settings.update_seconds,
            'closed_candles_only': user_settings.closed_candles_only,
            'automatic_pairs': user_settings.automatic_pairs,
            'api_keys_count': keys_count,
            'api_keys': [
                {'number': 1, 'configured': bool(user_settings.api_key_1)},
                {'number': 2, 'configured': bool(user_settings.api_key_2)},
                {'number': 3, 'configured': bool(user_settings.api_key_3)},
                {'number': 4, 'configured': bool(user_settings.api_key_4)},
            ]
        }
    })


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
    
    settings = get_settings()
    
    # Se o usuário enviou a máscara, significa que ele não alterou a chave, então mantemos a anterior
    if key_value == "••••••••••••••••" or not key_value:
        return jsonify({"success": True, "message": f"Chave 0{key_num} mantida com sucesso!"})

    if len(key_value) > 500:
        return api_error("A chave informada excede o tamanho permitido.")
        
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
    key = data.get("api_key", data.get("key", "")).strip()
    
    if not key or key == "••••••••••••••••":
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
    keys = get_user_api_keys(settings)
    config["api_keys_count"] = sum(1 for key in keys if key)
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


def background_analyzer():
    while True:
        try:
            with app.app_context():
                settings_list = UserSettings.query.filter_by(analysis_enabled=True).all()
                for settings in settings_list:
                    # Coleta de todas as chaves preenchidas e válidas
                    keys = [decrypt_secret(getattr(settings, f)) for f in API_KEY_FIELDS]
                    valid_keys = [k.strip() for k in keys if k and isinstance(k, str) and k.strip() != "••••••••••••••••"]
                    
                    if not valid_keys:
                        settings.last_analysis_error = "Nenhuma chave de API válida configurada para executar a análise."
                        db.session.commit()
                        continue
                    
                    ds = TwelveDataSource(api_keys=valid_keys)
                    pairs = settings.pairs_json or DEFAULT_PAIRS
                    
                    # Executa a busca dos candles
                    data = ds.get_candles_batch(pairs, interval=settings.timeframe, outputsize=settings.window_candles)
                    
                    if data and any(data.values()):
                        res = analyze_market(
                            market_data=data,
                            window=settings.window_candles,
                            leaders=settings.leaders,
                            followers_per_leader=settings.followers_per_leader,
                            min_similarity=settings.min_similarity
                        )
                        settings.last_results_json = res
                        settings.last_analysis_at = datetime.now(timezone.utc)
                        settings.last_analysis_error = None
                        db.session.commit()
                    else:
                        settings.last_analysis_error = ds.last_error or "Falha ao obter dados da Twelve Data (limite de requisições ou símbolo inválido)."
                        db.session.commit()
        except Exception as e:
            app.logger.exception('Erro na thread de análise: %s', e)
        time.sleep(30)
        
analyzer_thread = threading.Thread(target=background_analyzer, daemon=True)
analyzer_thread.start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)